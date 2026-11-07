from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import keyboards, texts
from bot.config import Config
from bot.delivery import deliver_day, deliver_if_due, send_lesson
from bot.services import (
    TOTAL_DAYS, delivered_map, due_time_for_next_day, get_or_create_user, grant_purchase,
    has_paid, reset_user, stats,
)

router = Router()


async def _user(session: AsyncSession, tg: TgUser, config: Config):
    user = await get_or_create_user(
        session, tg.id, tg.username, tg.first_name, config.default_timezone
    )
    await session.commit()
    return user


@router.message(CommandStart())
async def cmd_start(message: Message, sessionmaker: async_sessionmaker, config: Config) -> None:
    async with sessionmaker() as session:
        user = await _user(session, message.from_user, config)
        delivered = await delivered_map(session, user)
        paid = await has_paid(session, user)
    if not delivered:
        await message.answer(texts.WELCOME, reply_markup=keyboards.welcome())
        return
    done = len(delivered)
    if done >= TOTAL_DAYS:
        next_line = texts.STATUS_DONE
    elif not paid:
        next_line = texts.STATUS_NEED_PAY
    else:
        next_line = texts.STATUS_NEXT
    markup = keyboards.offer(config) if (not paid and done < TOTAL_DAYS) else keyboards.my_days_only()
    await message.answer(texts.STATUS.format(done=done, next_line=next_line), reply_markup=markup)


@router.callback_query(F.data == "about")
async def cb_about(call: CallbackQuery) -> None:
    await call.message.answer(texts.ABOUT.format(days=keyboards.days_list()))
    await call.message.answer(texts.DISCLAIMER, reply_markup=keyboards.about())
    await call.answer()


@router.callback_query(F.data == "day1")
async def cb_day1(
    call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker, config: Config
) -> None:
    await call.answer()
    async with sessionmaker() as session:
        user = await _user(session, call.from_user, config)
        sent = await deliver_day(bot, session, user, 1, config)
    if not sent:
        await call.message.answer(texts.DAY1_ALREADY, reply_markup=keyboards.my_days_only())


def _when_text(prev_at: datetime, tz_name: str, hour: int, now: datetime) -> str:
    due = due_time_for_next_day(prev_at, tz_name, hour)
    local_today = now.astimezone(ZoneInfo(tz_name)).date()
    when = "завтра" if due.date() == local_today + timedelta(days=1) else due.strftime("%d.%m")
    return when


@router.callback_query(F.data == "buy")
async def cb_buy(
    call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker, config: Config
) -> None:
    await call.answer()
    async with sessionmaker() as session:
        user = await _user(session, call.from_user, config)
        if await has_paid(session, user):
            await call.message.answer(texts.ALREADY_PAID)
            return
        if config.payment_mode != "test":
            user.wants_purchase = True
            await session.commit()
            await call.message.answer(texts.PAYMENT_CLOSED, reply_markup=keyboards.my_days_only())
            return

        await grant_purchase(
            session, user, provider="test", provider_payment_id=f"test-{user.tg_id}",
            amount=config.price_rub * 100,
        )
        await session.commit()

        delivered = await delivered_map(session, user)
        now = datetime.now(timezone.utc)
        if not delivered:
            # Оплатила, не открыв День 1: сразу выдаём его.
            await call.message.answer(texts.PAID_OK.format(when="") + texts.PAID_TEST_NOTE)
            await deliver_day(bot, session, user, 1, config)
            return
        last = max(delivered)
        nxt = last + 1
        due = due_time_for_next_day(delivered[last], user.timezone, config.delivery_hour)
        if now >= due:
            when = texts.NEXT_DAY_NOW.format(day=nxt)
        else:
            when = texts.NEXT_DAY_LATER.format(
                day=nxt, when=_when_text(delivered[last], user.timezone, config.delivery_hour, now),
                hour=config.delivery_hour, tz_label=texts.TZ_LABELS.get(user.timezone, user.timezone),
            )
        await call.message.answer(texts.PAID_OK.format(when=when) + "\n\n" + texts.PAID_TEST_NOTE)
        await deliver_if_due(bot, session, user, config, now)


async def _show_days(message: Message, tg: TgUser, sessionmaker, config: Config) -> None:
    async with sessionmaker() as session:
        user = await _user(session, tg, config)
        delivered = await delivered_map(session, user)
    if not delivered:
        await message.answer(texts.MY_DAYS_EMPTY, reply_markup=keyboards.about())
    else:
        await message.answer(texts.MY_DAYS, reply_markup=keyboards.days(list(delivered)))


@router.message(Command("days"))
async def cmd_days(message: Message, sessionmaker: async_sessionmaker, config: Config) -> None:
    await _show_days(message, message.from_user, sessionmaker, config)


@router.callback_query(F.data == "days")
async def cb_days(call: CallbackQuery, sessionmaker: async_sessionmaker, config: Config) -> None:
    await call.answer()
    await _show_days(call.message, call.from_user, sessionmaker, config)


@router.callback_query(F.data.startswith("open:"))
async def cb_open(
    call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker, config: Config
) -> None:
    await call.answer()
    try:
        day = int(call.data.split(":", 1)[1])
    except ValueError:
        return
    async with sessionmaker() as session:
        user = await _user(session, call.from_user, config)
        delivered = await delivered_map(session, user)
    if day not in delivered:
        await call.message.answer(texts.DAY_LOCKED)
        return
    await send_lesson(bot, call.from_user.id, day)


@router.message(Command("restore"))
async def cmd_restore(message: Message, sessionmaker: async_sessionmaker, config: Config) -> None:
    await message.answer(texts.RESTORE)
    await _show_days(message, message.from_user, sessionmaker, config)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


# --- Команды для тестирования (DEBUG_COMMANDS=true) ---

@router.message(Command("next"))
async def cmd_next(
    message: Message, bot: Bot, sessionmaker: async_sessionmaker, config: Config
) -> None:
    """Сразу открыть следующий день, не дожидаясь утра."""
    if not config.debug_commands:
        return
    async with sessionmaker() as session:
        user = await _user(session, message.from_user, config)
        delivered = await delivered_map(session, user)
        paid = await has_paid(session, user)
        nxt = (max(delivered) + 1) if delivered else 1
        if nxt > TOTAL_DAYS:
            await message.answer("Все 7 дней уже открыты. /reset — начать заново.")
            return
        if nxt > 1 and not paid:
            await message.answer("Дни 2–7 доступны после (тестовой) оплаты.")
            return
        await deliver_day(bot, session, user, nxt, config)


@router.message(Command("reset"))
async def cmd_reset(message: Message, sessionmaker: async_sessionmaker, config: Config) -> None:
    """Стереть свой прогресс и тестовую покупку."""
    if not config.debug_commands:
        return
    async with sessionmaker() as session:
        user = await _user(session, message.from_user, config)
        await reset_user(session, user)
        await session.commit()
    await message.answer("Прогресс сброшен. Нажми /start.")


@router.message(Command("stats"))
async def cmd_stats(message: Message, sessionmaker: async_sessionmaker, config: Config) -> None:
    if message.from_user.id not in config.admin_ids:
        return
    async with sessionmaker() as session:
        s = await stats(session)
    await message.answer(
        "<b>Воронка</b>\n"
        f"Нажали /start: {s['users']}\n"
        f"Открыли День 1: {s['started_day1']}\n"
        f"Хотели купить (оплата закрыта): {s['wants_purchase']}\n"
        f"Оплатили: {s['paid']}\n"
        f"Прошли все 7 дней: {s['completed']}\n"
        f"Заблокировали бота: {s['blocked']}"
    )
