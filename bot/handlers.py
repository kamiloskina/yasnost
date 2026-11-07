from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import keyboards, texts
from bot.config import Config
from bot.delivery import deliver_day, deliver_if_due, send_lesson
from bot.payments import send_invoice
from bot.services import (
    TOTAL_DAYS, TRACKER, delivered_map, due_time_for_next_day, get_or_create_user,
    grant_purchase, has_paid, reset_user, stats, stats_by_source,
)
from bot import legal
from bot.tracker import offer_tracker, send_tracker
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Kb

router = Router()


async def _user(session: AsyncSession, tg: TgUser, config: Config, source: str | None = None):
    user = await get_or_create_user(
        session, tg.id, tg.username, tg.first_name, config.default_timezone, source=source
    )
    await session.commit()
    return user


def _clean_source(arg: str | None) -> str | None:
    """t.me/Clarity7DaysBot?start=reels_01 → 'reels_01' (только латиница, цифры, _ и -)."""
    if not arg:
        return None
    cleaned = "".join(ch for ch in arg if ch.isascii() and (ch.isalnum() or ch in "_-"))
    return cleaned[:64] or None


@router.message(CommandStart())
async def cmd_start(
    message: Message, command: CommandObject, sessionmaker: async_sessionmaker, config: Config
) -> None:
    source = _clean_source(command.args)
    async with sessionmaker() as session:
        user = await _user(session, message.from_user, config, source)
        if source and source.startswith("tracker"):
            # Ссылка из канала на трекер: t.me/Clarity7DaysBot?start=tracker
            await offer_tracker(message.bot, session, user, config)
            return
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
    markup = (keyboards.offer(config, message.from_user.id) if (not paid and done < TOTAL_DAYS)
              else keyboards.my_days_only())
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
        if not await legal.has_pd_consent(session, user):
            await bot.send_message(
                call.from_user.id, texts.consent_request(config),
                reply_markup=Kb(inline_keyboard=[[Btn(text=texts.BTN_CONSENT, callback_data="consent")]]),
            )
            return
        delivered = await delivered_map(session, user)
        if 1 not in delivered:
            await legal.record(session, user, legal.FREE_ACCEPT)
            await session.commit()
        sent = await deliver_day(bot, session, user, 1, config)
    if not sent:
        await call.message.answer(texts.DAY1_ALREADY, reply_markup=keyboards.my_days_only())


@router.callback_query(F.data == "consent")
async def cb_consent(call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker, config: Config) -> None:
    """Отдельное действие: согласие на обработку ПДн (152-ФЗ, ст. 9). Не отмечено заранее."""
    await call.answer()
    async with sessionmaker() as session:
        user = await _user(session, call.from_user, config)
        if not await legal.has_pd_consent(session, user):
            await legal.record(session, user, legal.PD_CONSENT)
            await session.commit()
    await bot.send_message(call.from_user.id, texts.CONSENT_THANKS, reply_markup=keyboards.about())


def _when_text(prev_at: datetime, tz_name: str, hour: int, now: datetime) -> str:
    due = due_time_for_next_day(prev_at, tz_name, hour)
    local_today = now.astimezone(ZoneInfo(tz_name)).date()
    when = "завтра" if due.date() == local_today + timedelta(days=1) else due.strftime("%d.%m")
    return when


async def after_payment(
    bot: Bot, session: AsyncSession, user, config: Config, chat_id: int, test: bool
) -> None:
    """После подтверждённой оплаты: сообщить, когда придёт следующий день, и выдать его, если пора."""
    note = "\n\n" + texts.PAID_TEST_NOTE if test else ""
    delivered = await delivered_map(session, user)
    now = datetime.now(timezone.utc)
    if not delivered:
        # Оплатила, не открыв День 1: сразу выдаём его.
        await bot.send_message(chat_id, texts.PAID_OK.format(when=texts.NEXT_DAY_NOW.format(day=1)) + note)
        await deliver_day(bot, session, user, 1, config)
        return
    last = max(delivered)
    if last >= TOTAL_DAYS:
        await bot.send_message(chat_id, texts.PAID_OK.format(when="") + note)
        return
    nxt = last + 1
    due = due_time_for_next_day(delivered[last], user.timezone, config.delivery_hour)
    if now >= due:
        when = texts.NEXT_DAY_NOW.format(day=nxt)
    else:
        when = texts.NEXT_DAY_LATER.format(
            day=nxt, when=_when_text(delivered[last], user.timezone, config.delivery_hour, now),
            hour=config.delivery_hour, tz_label=texts.TZ_LABELS.get(user.timezone, user.timezone),
        )
    await bot.send_message(chat_id, texts.PAID_OK.format(when=when) + note)
    await deliver_if_due(bot, session, user, config, now)


def _payments_closed(config: Config, chat_id: int) -> bool:
    return config.payment_mode == "off" or (
        config.payment_mode == "test" and not config.test_access(chat_id)
    )


async def _ask_offer_accept(bot: Bot, chat_id: int, config: Config, product: str) -> None:
    if product == TRACKER:
        label, price = texts.TRACKER_NAME, texts.tracker_price_label(config)
    else:
        label, price = "Дни 2–7 пути «7 дней. Путь к Ясности»", texts.price_label(config)
    await bot.send_message(
        chat_id, texts.offer_confirm(config, label, price),
        reply_markup=Kb(inline_keyboard=[[Btn(text=texts.BTN_ACCEPT_OFFER, callback_data=f"accept:{product}")]]),
    )


@router.callback_query(F.data.startswith("accept:"))
async def cb_accept_offer(call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker, config: Config) -> None:
    """Отдельное действие: акцепт платной части оферты (ст. 438 ГК) перед оплатой."""
    await call.answer()
    product = call.data.split(":", 1)[1]
    async with sessionmaker() as session:
        user = await _user(session, call.from_user, config)
        if not await legal.has_offer_accept(session, user):
            await legal.record(session, user, legal.OFFER_ACCEPT, detail=product)
            await session.commit()
        if product == TRACKER:
            await _buy_tracker(bot, session, user, config, call.from_user.id)
        else:
            await _buy_course(bot, session, user, config, call.from_user.id)


@router.callback_query(F.data == "buy")
async def cb_buy(
    call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker, config: Config
) -> None:
    await call.answer()
    async with sessionmaker() as session:
        user = await _user(session, call.from_user, config)
        await _buy_course(bot, session, user, config, call.from_user.id)


async def _buy_course(bot: Bot, session: AsyncSession, user, config: Config, chat_id: int) -> None:
    if await has_paid(session, user):
        await bot.send_message(chat_id, texts.ALREADY_PAID)
        return
    if _payments_closed(config, chat_id):
        user.wants_purchase = True
        await session.commit()
        await bot.send_message(chat_id, texts.PAYMENT_CLOSED, reply_markup=keyboards.my_days_only())
        return
    if not await legal.has_offer_accept(session, user):
        await _ask_offer_accept(bot, chat_id, config, "course")
        return
    if config.payment_mode in {"yookassa", "stars"}:
        user.wants_purchase = True
        await session.commit()
        await send_invoice(bot, chat_id, config)
        return
    # test
    await grant_purchase(
        session, user, provider="test", provider_payment_id=f"test-{user.tg_id}",
        amount=config.price_rub * 100,
    )
    await session.commit()
    await after_payment(bot, session, user, config, chat_id, test=True)


async def _show_days(message: Message, tg: TgUser, sessionmaker, config: Config) -> None:
    async with sessionmaker() as session:
        user = await _user(session, tg, config)
        delivered = await delivered_map(session, user)
        has_tracker = await has_paid(session, user, TRACKER)
    if not delivered and not has_tracker:
        await message.answer(texts.MY_DAYS_EMPTY, reply_markup=keyboards.about())
    else:
        await message.answer(texts.MY_DAYS, reply_markup=keyboards.days(list(delivered), has_tracker))


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


@router.message(Command("tracker"))
async def cmd_tracker(
    message: Message, bot: Bot, sessionmaker: async_sessionmaker, config: Config
) -> None:
    async with sessionmaker() as session:
        user = await _user(session, message.from_user, config)
        await offer_tracker(bot, session, user, config)


@router.callback_query(F.data == "tracker")
async def cb_tracker(
    call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker, config: Config
) -> None:
    await call.answer()
    async with sessionmaker() as session:
        user = await _user(session, call.from_user, config)
        await offer_tracker(bot, session, user, config)


@router.callback_query(F.data == "buy:tracker")
async def cb_buy_tracker(
    call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker, config: Config
) -> None:
    await call.answer()
    async with sessionmaker() as session:
        user = await _user(session, call.from_user, config)
        await _buy_tracker(bot, session, user, config, call.from_user.id)


async def _buy_tracker(bot: Bot, session: AsyncSession, user, config: Config, chat_id: int) -> None:
    if await has_paid(session, user, TRACKER):
        await bot.send_message(chat_id, texts.TRACKER_ALREADY)
        await send_tracker(bot, chat_id)
        return
    if _payments_closed(config, chat_id):
        user.wants_purchase = True
        await session.commit()
        await bot.send_message(chat_id, texts.PAYMENT_CLOSED)
        return
    if not await legal.has_offer_accept(session, user):
        await _ask_offer_accept(bot, chat_id, config, TRACKER)
        return
    if config.payment_mode in {"yookassa", "stars"}:
        await send_invoice(bot, chat_id, config, TRACKER)
        return
    await grant_purchase(
        session, user, provider="test", provider_payment_id=f"test-tracker-{user.tg_id}",
        amount=config.tracker_price_rub * 100, product=TRACKER,
    )
    await session.commit()
    await bot.send_message(chat_id, texts.TRACKER_PAID_OK + "\n\n" + texts.PAID_TEST_NOTE)
    await send_tracker(bot, chat_id)


@router.message(Command("restore"))
async def cmd_restore(message: Message, sessionmaker: async_sessionmaker, config: Config) -> None:
    await message.answer(texts.RESTORE)
    await _show_days(message, message.from_user, sessionmaker, config)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


# --- Команды только для администраторов (ADMIN_IDS) ---

@router.message(Command("next"))
async def cmd_next(
    message: Message, bot: Bot, sessionmaker: async_sessionmaker, config: Config
) -> None:
    """Админ: сразу открыть следующий день, не дожидаясь утра."""
    if not config.is_admin(message.from_user.id):
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
    """Админ: стереть свой прогресс и покупки, чтобы пройти путь заново."""
    if not config.is_admin(message.from_user.id):
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
        sources = await stats_by_source(session)
    src_lines = "\n".join(f"  {src}: пришли {n} → День 1: {d} → оплатили {p}" for src, n, d, p in sources)
    await message.answer(
        "<b>Воронка</b>\n"
        f"Нажали /start: {s['users']}\n"
        f"Открыли День 1: {s['started_day1']}\n"
        f"Нажали «купить»: {s['wants_purchase']}\n"
        f"Оплатили путь: {s['paid']}\n"
        f"Купили трекер отдельно: {s['tracker_paid']}\n"
        f"Получили трекер в подарок: {s['tracker_gift']}\n"
        f"Прошли все 7 дней: {s['completed']}\n"
        f"Заблокировали бота: {s['blocked']}\n\n"
        f"<b>Источники</b> (?start=…)\n{src_lines or '  пока нет'}"
    )


@router.message(Command("autopost"))
async def cmd_autopost(
    message: Message, bot: Bot, sessionmaker: async_sessionmaker, config: Config
) -> None:
    """Админ: состояние автопостинга и ближайшие посты."""
    if message.from_user.id not in config.admin_ids:
        return
    from bot.channel import autopost_status
    await message.answer(await autopost_status(bot, sessionmaker, config))


@router.message(Command("history"))
async def cmd_history(
    message: Message, command: CommandObject, sessionmaker: async_sessionmaker, config: Config
) -> None:
    """Админ: /history <telegram_id> — оплаты и время выдачи каждого дня (доказательство при спорах)."""
    if not config.is_admin(message.from_user.id):
        return
    from sqlalchemy import select

    from bot.models import Purchase, User
    from bot.services import as_utc
    try:
        tg_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Формат: /history 123456789 (Telegram ID)")
        return
    tz = ZoneInfo(config.default_timezone)
    async with sessionmaker() as session:
        user = await session.scalar(select(User).where(User.tg_id == tg_id))
        if user is None:
            await message.answer("Такого пользователя в базе нет.")
            return
        purchases = list(await session.scalars(
            select(Purchase).where(Purchase.user_id == user.id).order_by(Purchase.created_at)
        ))
        delivered = await delivered_map(session, user)
    name = f"@{user.username}" if user.username else (user.first_name or str(tg_id))
    lines = [f"<b>{name}</b> (ID {tg_id})", f"Пришла: {as_utc(user.created_at).astimezone(tz):%d.%m.%Y %H:%M}"
             + (f", источник: {user.source}" if user.source else "")]
    lines.append("\n<b>Оплаты</b>")
    if not purchases:
        lines.append("— нет")
    for p in purchases:
        amount = f"{p.amount / 100:.0f} ₽" if p.currency == "RUB" else f"{p.amount} {p.currency}"
        lines.append(f"— {as_utc(p.created_at).astimezone(tz):%d.%m.%Y %H:%M}: {p.product}, {amount}, "
                     f"{p.provider}, статус: {p.status}")
    lines.append("\n<b>Выданные дни</b> (время отправки в чат)")
    if not delivered:
        lines.append("— нет")
    for d in sorted(delivered):
        lines.append(f"— День {d}: {delivered[d].astimezone(tz):%d.%m.%Y %H:%M}")
    await message.answer("\n".join(lines))


@router.message(Command("forget"))
async def cmd_forget(
    message: Message, command: CommandObject, sessionmaker: async_sessionmaker, config: Config
) -> None:
    """Админ: /forget <telegram_id> — удалить данные по запросу человека (152-ФЗ).
    Прогресс и профиль стираются; сведения об оплатах остаются (их нужно хранить по закону)."""
    if not config.is_admin(message.from_user.id):
        return
    from sqlalchemy import delete, select

    from bot.models import Delivery, User
    try:
        tg_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Формат: /forget 123456789 (Telegram ID)")
        return
    async with sessionmaker() as session:
        user = await session.scalar(select(User).where(User.tg_id == tg_id))
        if user is None:
            await message.answer("Такого пользователя в базе нет.")
            return
        await session.execute(delete(Delivery).where(Delivery.user_id == user.id))
        user.username = None
        user.first_name = None
        user.source = None
        user.wants_purchase = False
        await session.commit()
    await message.answer(
        "Удалила имя, ник, источник и прогресс. Сведения об оплатах сохранены — "
        "их нужно хранить для налоговой. Напиши человеку, что запрос выполнен."
    )
