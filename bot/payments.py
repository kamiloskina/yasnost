"""Реальная оплата через Telegram Payments: ЮKassa (рубли) или Telegram Stars.

Поток: кнопка «Продолжить путь» → счёт (send_invoice) → Telegram спрашивает бота
pre_checkout_query (можно ли продавать) → покупатель платит → приходит successful_payment →
запись покупки (идемпотентно по telegram_payment_charge_id) → открытие дней 2–7.
"""

import json
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot import texts
from bot.config import Config
from bot.models import User
from bot.services import get_or_create_user, grant_purchase, has_paid, refund_user

log = logging.getLogger(__name__)
router = Router()

PAYLOAD = "course7:days2-7"


def invoice_params(config: Config) -> dict:
    """Параметры счёта для send_invoice в зависимости от режима оплаты."""
    title = texts.COURSE
    description = texts.INVOICE_DESCRIPTION
    if config.payment_mode == "stars":
        return dict(
            title=title, description=description, payload=PAYLOAD, currency="XTR",
            prices=[LabeledPrice(label=texts.INVOICE_LABEL, amount=config.stars_price)],
        )
    if config.payment_mode == "yookassa":
        params = dict(
            title=title, description=description, payload=PAYLOAD, currency="RUB",
            provider_token=config.yookassa_provider_token,
            prices=[LabeledPrice(label=texts.INVOICE_LABEL, amount=config.price_rub * 100)],
        )
        if config.yookassa_send_receipt:
            # Чек по 54-ФЗ через ЮKassa: покупатель указывает e-mail, ЮKassa отправляет чек.
            params.update(
                need_email=True,
                send_email_to_provider=True,
                provider_data=json.dumps({"receipt": {"items": [{
                    "description": f"{texts.COURSE}: {texts.INVOICE_LABEL}"[:128],
                    "quantity": "1.00",
                    "amount": {"value": f"{config.price_rub:.2f}", "currency": "RUB"},
                    "vat_code": 1,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }]}}, ensure_ascii=False),
            )
        return params
    raise ValueError(f"invoice for payment_mode={config.payment_mode} is not supported")


async def send_invoice(bot: Bot, chat_id: int, config: Config) -> None:
    await bot.send_message(chat_id, texts.before_invoice(config))
    await bot.send_invoice(chat_id, **invoice_params(config))


@router.pre_checkout_query()
async def on_pre_checkout(
    query: PreCheckoutQuery, sessionmaker: async_sessionmaker, config: Config
) -> None:
    if query.invoice_payload != PAYLOAD or config.payment_mode not in {"yookassa", "stars"}:
        await query.answer(ok=False, error_message=texts.PAYMENT_UNAVAILABLE)
        return
    async with sessionmaker() as session:
        user = await session.scalar(select(User).where(User.tg_id == query.from_user.id))
        if user is not None and await has_paid(session, user):
            await query.answer(ok=False, error_message=texts.ALREADY_PAID_SHORT)
            return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(
    message: Message, bot: Bot, sessionmaker: async_sessionmaker, config: Config
) -> None:
    from bot.handlers import after_payment  # избегаем циклического импорта

    sp = message.successful_payment
    provider = "stars" if sp.currency == "XTR" else "yookassa"
    async with sessionmaker() as session:
        user = await get_or_create_user(
            session, message.from_user.id, message.from_user.username,
            message.from_user.first_name, config.default_timezone,
        )
        await session.commit()
        created = await grant_purchase(
            session, user, provider=provider,
            provider_payment_id=sp.telegram_payment_charge_id,
            amount=sp.total_amount, currency=sp.currency,
        )
        await session.commit()
        log.info(
            "payment %s from %s: %s %s (provider charge %s), new=%s", provider,
            message.from_user.id, sp.total_amount, sp.currency, sp.provider_payment_charge_id, created,
        )
        if not created:
            await message.answer(texts.ALREADY_PAID)
            return
        await after_payment(bot, session, user, config, message.chat.id, test=False)


@router.message(Command("refund"))
async def cmd_refund(
    message: Message, command: CommandObject, bot: Bot, sessionmaker: async_sessionmaker,
    config: Config,
) -> None:
    """Админ: /refund <telegram_id> — вернуть оплату и закрыть дни 2–7."""
    if message.from_user.id not in config.admin_ids:
        return
    try:
        tg_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Формат: /refund 123456789 (Telegram ID покупательницы)")
        return
    async with sessionmaker() as session:
        user = await session.scalar(select(User).where(User.tg_id == tg_id))
        if user is None:
            await message.answer("Такого пользователя в базе нет.")
            return
        purchases = await refund_user(session, user)
        if not purchases:
            await message.answer("Оплаченных покупок у пользователя нет.")
            return
        notes = []
        for p in purchases:
            if p.provider == "stars":
                try:
                    await bot.refund_star_payment(tg_id, p.provider_payment_id)
                    notes.append(f"Звёзды ({p.amount}) возвращены автоматически.")
                except Exception as e:  # уже возвращено или ошибка Telegram
                    await session.rollback()
                    await message.answer(f"Telegram не принял возврат звёзд: {e}")
                    return
            elif p.provider == "yookassa":
                notes.append(
                    f"Оплата ЮKassa {p.amount / 100:.0f} ₽ помечена как возвращённая. "
                    "Сам возврат денег сделай в личном кабинете ЮKassa."
                )
            else:
                notes.append(f"Покупка «{p.provider}» помечена как возвращённая.")
        await session.commit()
    await message.answer("\n".join(notes) + "\nВыдача следующих дней остановлена.")
    try:
        await bot.send_message(tg_id, texts.REFUNDED)
    except Exception:
        log.info("could not notify %s about refund", tg_id)


@router.message(Command("terms"))
async def cmd_terms(message: Message, config: Config) -> None:
    await message.answer(texts.terms(config))


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message, config: Config) -> None:
    await message.answer(texts.paysupport(config))
