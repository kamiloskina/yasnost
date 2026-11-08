"""Отказ от договора и возврат (ст. 32 ЗоЗПП, ст. 782 ГК РФ) и управление персональными данными.

Процедура возврата:
1. Пользователь: /return → бот показывает расчёт → «Подтверждаю отказ» (отдельное действие).
2. Заявление регистрируется (номер, дата, сколько платных этапов направлено, сумма к возврату),
   выдача следующих этапов прекращается. Администраторы получают уведомление.
3. Администратор проводит возврат в ЮKassa (или Telegram возвращает звёзды автоматически,
   если ни один платный этап не направлен) и фиксирует результат: /refund_done <№> [сумма ₽].
4. Пользователь получает уведомление; результат сохраняется в базе.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Kb
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import legal, texts
from bot.config import Config
from bot.models import Purchase, RefundRequest, User
from bot.services import COURSE, TRACKER, get_or_create_user

log = logging.getLogger(__name__)
router = Router()


async def _user(session: AsyncSession, message_or_call, config: Config) -> User:
    tg = message_or_call.from_user
    user = await get_or_create_user(session, tg.id, tg.username, tg.first_name, config.default_timezone)
    await session.commit()
    return user


async def _active_course_purchase(session: AsyncSession, user: User) -> Purchase | None:
    return await session.scalar(
        select(Purchase).where(
            Purchase.user_id == user.id, Purchase.product == COURSE,
            Purchase.status.in_(("paid", "refund_pending")), Purchase.provider != "gift",
        ).order_by(Purchase.id.desc()).limit(1)
    )


async def _notify_admins(bot: Bot, config: Config, text: str) -> None:
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            log.exception("cannot notify admin %s", admin_id)


async def _register_and_notify(bot: Bot, session: AsyncSession, user: User, purchase: Purchase,
                               config: Config) -> RefundRequest:
    req, created = await legal.register_refund(session, user, purchase)
    await session.commit()
    due = legal.fmt_amount(req.amount_due, req.currency)
    if not created:
        return req
    # Звёзды Telegram возвращает только целиком: автоматически - только если платных этапов не было
    if purchase.provider == "stars" and req.stages_provided == 0:
        try:
            await bot.refund_star_payment(user.tg_id, purchase.provider_payment_id)
            await legal.complete_refund(session, req)
            await session.commit()
            await bot.send_message(user.tg_id, texts.refund_done_user(due))
            await _notify_admins(bot, config, f"Возврат №{req.id}: звёзды ({due}) возвращены автоматически.")
            return req
        except Exception as e:
            log.exception("stars refund failed for request %s", req.id)
            await _notify_admins(bot, config, f"⚠️ Возврат №{req.id}: Telegram не вернул звёзды: {e}")
    who = f"ID {user.tg_id}"
    paid = legal.fmt_amount(purchase.amount, purchase.currency)
    how = {
        "yookassa": "Верни сумму в личном кабинете ЮKassa, затем скорректируй чек в «Мой налог».",
        "stars": "⚠️ Частичный возврат звёзд в Telegram невозможен - реши вопрос с юристом (LEGAL_REVIEW).",
    }.get(purchase.provider, "Тестовая покупка - деньги не списывались.")
    await _notify_admins(
        bot, config,
        f"🧾 Заявление на возврат №{req.id}\nОт: {who}\n"
        f"Оплачено: {paid}, способ: {purchase.provider}\n"
        f"Направлено платных этапов: {req.stages_provided} из 6\nК возврату по оферте: {due}\n\n"
        f"{how}\nПосле возврата: /refund_done {req.id}  (или /refund_done {req.id} <сумма ₽>, "
        "если возвращаешь другую сумму - например, человек сообщил, что этап к нему не пришёл).",
    )
    return req


# ---------- пользователь ----------

@router.message(Command("return"))
async def cmd_return(message: Message, sessionmaker: async_sessionmaker, config: Config) -> None:
    async with sessionmaker() as session:
        user = await _user(session, message, config)
        purchase = await _active_course_purchase(session, user)
        if purchase is None:
            await message.answer(texts.RETURN_NOTHING)
            return
        req = await legal.open_request(session, purchase)
        if req is not None:
            await message.answer(texts.return_registered(req.id, legal.fmt_amount(req.amount_due, req.currency)))
            return
        stages = await legal.stages_provided(session, user)
        due = legal.refund_amount(purchase.amount, stages)
    await message.answer(
        texts.return_calc(stages, legal.fmt_amount(purchase.amount, purchase.currency),
                          legal.fmt_amount(due, purchase.currency)),
        reply_markup=Kb(inline_keyboard=[[Btn(text=texts.BTN_RETURN_CONFIRM, callback_data=f"ret:{purchase.id}")]]),
    )


@router.callback_query(F.data.startswith("ret:"))
async def cb_return_confirm(call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker,
                            config: Config) -> None:
    await call.answer()
    async with sessionmaker() as session:
        user = await _user(session, call, config)
        purchase = await session.get(Purchase, int(call.data.split(":", 1)[1]))
        if purchase is None or purchase.user_id != user.id or purchase.status not in ("paid", "refund_pending"):
            await bot.send_message(call.from_user.id, texts.RETURN_NOTHING)
            return
        req = await _register_and_notify(bot, session, user, purchase, config)
        if req.status == "registered":
            await bot.send_message(call.from_user.id,
                                   texts.return_registered(req.id, legal.fmt_amount(req.amount_due, req.currency)))


# ---------- администратор ----------

@router.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject, bot: Bot,
                     sessionmaker: async_sessionmaker, config: Config) -> None:
    """Админ: /refund <telegram_id> [tracker] - зарегистрировать отказ от имени пользователя
    (например, если он написал на почту). Расчёт - тот же, что в /return."""
    if not config.is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    try:
        tg_id = int(parts[0])
        product = TRACKER if len(parts) > 1 and parts[1].lower() == TRACKER else COURSE
    except (ValueError, IndexError):
        await message.answer("Формат: /refund 123456789 - путь, /refund 123456789 tracker - трекер")
        return
    async with sessionmaker() as session:
        user = await session.scalar(select(User).where(User.tg_id == tg_id))
        if user is None:
            await message.answer("Такого пользователя в базе нет.")
            return
        purchase = await session.scalar(
            select(Purchase).where(Purchase.user_id == user.id, Purchase.product == product,
                                   Purchase.status.in_(("paid", "refund_pending")),
                                   Purchase.provider != "gift").order_by(Purchase.id.desc()).limit(1)
        )
        if purchase is None:
            await message.answer("Оплаченных покупок у пользователя нет.")
            return
        if product == TRACKER:
            # Трекер - файл: сумма возврата определяется по обстоятельствам (не дошёл, дефект и т.п.)
            req, _ = await legal.register_refund(session, user, purchase)
            req.amount_due = purchase.amount
            await session.commit()
            await message.answer(
                f"Заявление №{req.id} по трекеру зарегистрировано. Если возвращаешь - проведи возврат и "
                f"напиши /refund_done {req.id} (полная сумма {legal.fmt_amount(purchase.amount, purchase.currency)}) "
                f"или /refund_done {req.id} <сумма ₽>."
            )
            return
        req = await _register_and_notify(bot, session, user, purchase, config)
    if req.status == "registered":
        await message.answer(f"Заявление №{req.id} зарегистрировано, выдача следующих этапов остановлена.")
        try:
            await bot.send_message(tg_id, texts.return_registered(req.id, legal.fmt_amount(req.amount_due, req.currency)))
        except Exception:
            log.info("could not notify %s about refund request", tg_id)


@router.message(Command("refund_done"))
async def cmd_refund_done(message: Message, command: CommandObject, bot: Bot,
                          sessionmaker: async_sessionmaker, config: Config) -> None:
    """Админ: /refund_done <№ заявления> [сумма ₽] - зафиксировать выполненный возврат."""
    if not config.is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    try:
        req_id = int(parts[0])
        final = int(round(float(parts[1].replace(",", ".")) * 100)) if len(parts) > 1 else None
    except (ValueError, IndexError):
        await message.answer("Формат: /refund_done 5 или /refund_done 5 825")
        return
    async with sessionmaker() as session:
        req = await session.get(RefundRequest, req_id)
        if req is None:
            await message.answer("Заявления с таким номером нет.")
            return
        if req.currency != "RUB" and final is not None:
            final = final // 100  # для звёзд сумма указывается в звёздах
        done = await legal.complete_refund(session, req, final)
        await session.commit()
        user = await session.get(User, req.user_id)
        amount = legal.fmt_amount(req.final_amount, req.currency)
    if not done:
        await message.answer(f"Заявление №{req_id} уже закрыто ранее ({amount}). Повторно ничего не делаю.")
        return
    await message.answer(f"Возврат №{req_id} на {amount} зафиксирован ✓ Не забудь скорректировать чек в «Мой налог».")
    try:
        await bot.send_message(user.tg_id, texts.refund_done_user(amount))
    except Exception:
        log.info("could not notify user about refund %s", req_id)
