"""Реальная оплата через Telegram Payments: ЮKassa (рубли) или Telegram Stars.

Поток: кнопка «Продолжить путь» → счёт (send_invoice) → Telegram спрашивает бота
pre_checkout_query (можно ли продавать) → покупатель платит → приходит successful_payment →
запись покупки (идемпотентно по telegram_payment_charge_id) → открытие дней 2-7.
"""

import json
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot import texts
from bot.config import Config
from bot.models import User
from bot.services import COURSE, TRACKER, get_or_create_user, grant_purchase, has_paid

log = logging.getLogger(__name__)
router = Router()

PAYLOAD = "course7:days2-7"
TRACKER_PAYLOAD = "tracker30"
PAYLOADS = {PAYLOAD: COURSE, TRACKER_PAYLOAD: TRACKER}


def _product_info(config: Config, product: str) -> dict:
    if product == TRACKER:
        return dict(
            title=texts.TRACKER_NAME, description=texts.TRACKER_INVOICE_DESCRIPTION,
            label=texts.TRACKER_INVOICE_LABEL, payload=TRACKER_PAYLOAD,
            rub=config.tracker_price_rub, stars=config.tracker_stars_price,
        )
    return dict(
        title=texts.COURSE, description=texts.INVOICE_DESCRIPTION, label=texts.INVOICE_LABEL,
        payload=PAYLOAD, rub=config.price_rub, stars=config.stars_price,
    )


def invoice_params(config: Config, product: str = COURSE) -> dict:
    """Параметры счёта для send_invoice в зависимости от режима оплаты и продукта."""
    info = _product_info(config, product)
    title, description, label = info["title"], info["description"], info["label"]
    if config.payment_mode == "stars":
        return dict(
            title=title, description=description, payload=info["payload"], currency="XTR",
            prices=[LabeledPrice(label=label, amount=info["stars"])],
        )
    if config.payment_mode == "yookassa":
        params = dict(
            title=title, description=description, payload=info["payload"], currency="RUB",
            provider_token=config.yookassa_provider_token,
            prices=[LabeledPrice(label=label, amount=info["rub"] * 100)],
        )
        if config.yookassa_send_receipt:
            # Чек по 54-ФЗ через ЮKassa: покупатель указывает e-mail, ЮKassa отправляет чек.
            params.update(
                need_email=True,
                send_email_to_provider=True,
                provider_data=json.dumps({"receipt": {"items": [{
                    "description": f"{title}: {label}"[:128],
                    "quantity": "1.00",
                    "amount": {"value": f"{info['rub']:.2f}", "currency": "RUB"},
                    "vat_code": 1,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }]}}, ensure_ascii=False),
            )
        return params
    raise ValueError(f"invoice for payment_mode={config.payment_mode} is not supported")


async def send_invoice(bot: Bot, chat_id: int, config: Config, product: str = COURSE) -> None:
    if product == COURSE:
        await bot.send_message(chat_id, texts.before_invoice(config))
    await bot.send_invoice(chat_id, **invoice_params(config, product))


@router.pre_checkout_query()
async def on_pre_checkout(
    query: PreCheckoutQuery, sessionmaker: async_sessionmaker, config: Config
) -> None:
    product = PAYLOADS.get(query.invoice_payload)
    if product == COURSE and not config.course_sales:
        product = None  # продажи курса закрыты (COURSE_SALES=false)
    if product is None or config.payment_mode not in {"yookassa", "stars"}:
        await query.answer(ok=False, error_message=texts.PAYMENT_UNAVAILABLE)
        return
    async with sessionmaker() as session:
        user = await session.scalar(select(User).where(User.tg_id == query.from_user.id))
        if user is not None and await has_paid(session, user, product):
            await query.answer(ok=False, error_message=texts.ALREADY_PAID_SHORT)
            return
        from bot import legal
        if user is None or not await legal.has_offer_accept(session, user):
            await query.answer(ok=False, error_message=texts.OFFER_NOT_ACCEPTED)
            return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(
    message: Message, bot: Bot, sessionmaker: async_sessionmaker, config: Config
) -> None:
    from bot.handlers import after_payment  # избегаем циклического импорта

    from bot.tracker import send_tracker

    sp = message.successful_payment
    provider = "stars" if sp.currency == "XTR" else "yookassa"
    product = PAYLOADS.get(sp.invoice_payload, COURSE)
    async with sessionmaker() as session:
        user = await get_or_create_user(
            session, message.from_user.id, message.from_user.username,
            message.from_user.first_name, config.default_timezone,
        )
        await session.commit()
        created = await grant_purchase(
            session, user, provider=provider,
            provider_payment_id=sp.telegram_payment_charge_id,
            amount=sp.total_amount, currency=sp.currency, product=product,
        )
        await session.commit()
        log.info(
            "payment %s from %s: %s %s (provider charge %s), new=%s", provider,
            message.from_user.id, sp.total_amount, sp.currency, sp.provider_payment_charge_id, created,
        )
        if not created:
            await message.answer(texts.ALREADY_PAID if product == COURSE else texts.TRACKER_ALREADY)
            return
        if product == TRACKER:
            await message.answer(texts.TRACKER_PAID_OK)
            await send_tracker(bot, message.chat.id)
            return
        await after_payment(bot, session, user, config, message.chat.id, test=False)


@router.message(Command("terms"))
async def cmd_terms(message: Message, config: Config) -> None:
    await message.answer(texts.terms(config))

