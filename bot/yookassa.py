"""Оплата на странице ЮKassa через API (PAYMENT_MODE=yookassa_api).

1. Бот создаёт платёж (POST /v3/payments) с номером заказа в metadata.order и присылает
   ссылку на оплату. Telegram ID в ЮKassa не передаётся.
2. Статус проверяется запросом к ЮKassa (GET /v3/payments/{id}): раз в минуту
   планировщиком и сразу по кнопке «Я оплатила - проверить». Сообщение пользователя
   «я оплатила» подтверждением не считается - только статус succeeded от ЮKassa.
3. При succeeded (и совпадении суммы, валюты и номера заказа) покупка записывается
   идемпотентно (уникальный id платежа) и выдаётся курс или трекер.
"""

import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Kb
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import texts
from bot.config import Config
from bot.models import PendingPayment, User
from bot.services import COURSE, TRACKER, as_utc, grant_purchase

log = logging.getLogger(__name__)
router = Router()

API = "https://api.yookassa.ru/v3"
REUSE_MINUTES = 50          # повторно показываем ту же ссылку, если платёж ещё свежий
EXPIRE_HOURS = 24           # неоплаченный платёж старше суток больше не проверяем

DESCRIPTIONS = {
    COURSE: "Доступ к онлайн-программе «7 дней. Путь к Ясности» (дни 2-7)",
    TRACKER: "Трекер «30 дней внимания к себе» (PDF)",
}


class YooKassaError(Exception):
    pass


class YooKassaClient:
    def __init__(self, shop_id: str, secret_key: str):
        self._auth = aiohttp.BasicAuth(shop_id, secret_key)

    async def _request(self, method: str, path: str, json: dict | None = None,
                       idempotence_key: str | None = None) -> dict:
        headers = {"Idempotence-Key": idempotence_key} if idempotence_key else {}
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(auth=self._auth, timeout=timeout) as http:
            async with http.request(method, API + path, json=json, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 300:
                    raise YooKassaError(f"{resp.status}: {data}")
                return data

    async def create_payment(self, amount_kop: int, description: str, order_id: int, return_url: str) -> dict:
        body = {
            "amount": {"value": f"{amount_kop / 100:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:128],
            "metadata": {"order": str(order_id)},
        }
        return await self._request("POST", "/payments", body, idempotence_key=f"order-{order_id}")

    async def get_payment(self, payment_id: str) -> dict:
        return await self._request("GET", f"/payments/{payment_id}")


def make_client(config: Config) -> YooKassaClient:
    """Точка подмены в тестах."""
    return YooKassaClient(config.yookassa_shop_id, config.yookassa_secret_key)


async def _price(session: AsyncSession, config: Config, product: str) -> int:
    if product == TRACKER:
        return config.tracker_price_rub * 100
    from bot.presale import course_price
    return (await course_price(session, config)).rub * 100


def _pay_markup(pending: PendingPayment, price_label: str) -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text=texts.BTN_PAY.format(price=price_label), url=pending.confirmation_url)],
        [Btn(text=texts.BTN_PAY_CHECK, callback_data=f"paycheck:{pending.id}")],
    ])


async def start_payment(bot: Bot, session: AsyncSession, user: User, config: Config,
                        chat_id: int, product: str) -> None:
    """Создаёт платёж (или показывает свежий неоплаченный) и присылает ссылку на оплату."""
    from bot.legal import fmt_amount, stage_price
    now = datetime.now(timezone.utc)
    recent = await session.scalar(
        select(PendingPayment).where(
            PendingPayment.user_id == user.id, PendingPayment.product == product,
            PendingPayment.status == "pending", PendingPayment.confirmation_url.is_not(None),
        ).order_by(PendingPayment.id.desc()).limit(1)
    )
    if recent is not None and as_utc(recent.created_at) > now - timedelta(minutes=REUSE_MINUTES):
        # Свежая ссылка действует по своей цене (даже если места предзаказа уже закончились)
        await bot.send_message(chat_id, texts.pay_link(product, fmt_amount(stage_price(recent.amount), "RUB")),
                               reply_markup=_pay_markup(recent, fmt_amount(recent.amount, "RUB")))
        return
    pending = PendingPayment(user_id=user.id, product=product, amount=await _price(session, config, product))
    session.add(pending)
    await session.flush()
    try:
        data = await make_client(config).create_payment(
            pending.amount, DESCRIPTIONS[product], pending.id, config.return_url)
        pending.payment_id = data["id"]
        pending.confirmation_url = data["confirmation"]["confirmation_url"]
    except Exception:
        log.exception("yookassa: cannot create payment for order %s", pending.id)
        pending.status = "canceled"
        await session.commit()
        await bot.send_message(chat_id, texts.PAY_CREATE_FAILED)
        return
    await session.commit()
    await bot.send_message(chat_id, texts.pay_link(product, fmt_amount(stage_price(pending.amount), "RUB")),
                           reply_markup=_pay_markup(pending, fmt_amount(pending.amount, "RUB")))


async def check_payment(bot: Bot, session: AsyncSession, pending: PendingPayment, config: Config) -> str:
    """Спрашивает у ЮKassa статус. Возвращает pending | succeeded | canceled | error."""
    if pending.status != "pending" or not pending.payment_id:
        return pending.status
    try:
        data = await make_client(config).get_payment(pending.payment_id)
    except Exception:
        log.exception("yookassa: cannot check payment for order %s", pending.id)
        return "error"
    status = data.get("status")
    if status == "canceled":
        pending.status = "canceled"
        await session.commit()
        return "canceled"
    if status != "succeeded" or not data.get("paid"):
        return "pending"
    amount = data.get("amount", {})
    if (amount.get("currency") != "RUB" or round(float(amount.get("value", 0)) * 100) != pending.amount
            or str(data.get("metadata", {}).get("order")) != str(pending.id)):
        log.error("yookassa: payment %s does not match order %s", pending.payment_id, pending.id)
        return "error"
    user = await session.get(User, pending.user_id)
    created = await grant_purchase(session, user, provider="yookassa", provider_payment_id=pending.payment_id,
                                   amount=pending.amount, currency="RUB", product=pending.product)
    pending.status = "succeeded"
    await session.commit()
    if created:
        await _deliver(bot, session, user, config, pending.product)
        await _notify_receipt(bot, config, pending, data)
    return "succeeded"


async def _deliver(bot: Bot, session: AsyncSession, user: User, config: Config, product: str) -> None:
    from bot.handlers import after_payment
    from bot.tracker import send_tracker
    if product == TRACKER:
        await bot.send_message(user.tg_id, texts.TRACKER_PAID_OK)
        await send_tracker(bot, user.tg_id)
    else:
        await after_payment(bot, session, user, config, user.tg_id, test=False)


async def _notify_receipt(bot: Bot, config: Config, pending: PendingPayment, data: dict) -> None:
    """Напоминание администраторам: пробить чек в «Мой налог» (ст. 14 422-ФЗ).
    ЮKassa может не формировать чеки самозанятых автоматически."""
    paid_at = data.get("captured_at") or data.get("created_at") or ""
    when = paid_at[:16].replace("T", " ") + " UTC" if paid_at else "сейчас"
    text = texts.receipt_reminder(DESCRIPTIONS[pending.product], pending.amount / 100, when, pending.payment_id)
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            log.exception("cannot send receipt reminder to %s", admin_id)


async def poll_pending(bot: Bot, sessionmaker: async_sessionmaker, config: Config) -> None:
    """Раз в минуту: проверить неоплаченные платежи за последние сутки, старые - закрыть."""
    now = datetime.now(timezone.utc)
    async with sessionmaker() as session:
        rows = list(await session.scalars(select(PendingPayment).where(PendingPayment.status == "pending")))
    for row in rows:
        async with sessionmaker() as session:
            pending = await session.get(PendingPayment, row.id)
            if as_utc(pending.created_at) < now - timedelta(hours=EXPIRE_HOURS):
                pending.status = "canceled"
                await session.commit()
                continue
            await check_payment(bot, session, pending, config)


@router.callback_query(F.data.startswith("paycheck:"))
async def cb_paycheck(call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker, config: Config) -> None:
    await call.answer()
    async with sessionmaker() as session:
        pending = await session.get(PendingPayment, int(call.data.split(":", 1)[1]))
        user = await session.scalar(select(User).where(User.tg_id == call.from_user.id))
        if pending is None or user is None or pending.user_id != user.id:
            return
        status = await check_payment(bot, session, pending, config)
    if status == "pending":
        await bot.send_message(call.from_user.id, texts.PAY_NOT_YET)
    elif status == "canceled":
        await bot.send_message(call.from_user.id, texts.PAY_CANCELED)
    elif status == "error":
        await bot.send_message(call.from_user.id, texts.PAY_CHECK_ERROR)
