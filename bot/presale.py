"""Предзаказ дней 2-7 и дата старта платных этапов.

- PRESALE_PRICE_RUB и PRESALE_LIMIT: первые N оплат курса - по цене предзаказа. Счётчик честный:
  считаются реальные оплаты курса (тестовые и подарочные не считаются, возвраты место не освобождают).
  Ссылка на оплату, выданная до того, как места закончились, действует по своей цене.
- COURSE_START: раньше этой даты (DELIVERY_HOUR по часовому поясу пользователя) платные этапы
  не отправляются. С этой даты каждый платный этап уходит только вместе с аудиопрактикой: если
  записи ещё нет, этап ждёт её, а администратор получает предупреждение.
- /notify_waitlist: одно сообщение тем, кто нажимал «Продолжить путь», пока оплата была закрыта.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Kb
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import texts
from bot.config import PAID_MODES, Config
from bot.models import Purchase, User
from bot.services import COURSE

log = logging.getLogger(__name__)
router = Router()

NOT_COUNTED = ("test", "gift")


@dataclass(frozen=True)
class CoursePrice:
    rub: int          # цена дней 2-7 сейчас
    presale: bool     # действует цена предзаказа
    left: int         # сколько мест по цене предзаказа осталось (0 - предзаказа нет)


def enabled(config: Config) -> bool:
    return config.presale_price_rub > 0 and config.presale_limit > 0 and config.payment_mode != "stars"


async def sold(session: AsyncSession) -> int:
    """Сколько реальных оплат курса было (включая возвращённые: место не освобождается)."""
    return int(await session.scalar(
        select(func.count()).select_from(Purchase).where(
            Purchase.product == COURSE, Purchase.provider.not_in(NOT_COUNTED))
    ) or 0)


async def course_price(session: AsyncSession, config: Config) -> CoursePrice:
    if enabled(config):
        left = config.presale_limit - await sold(session)
        if left > 0:
            return CoursePrice(config.presale_price_rub, True, left)
    return CoursePrice(config.price_rub, False, 0)


def start_at(config: Config, tz_name: str) -> datetime | None:
    """Момент старта платных этапов (UTC): COURSE_START в DELIVERY_HOUR по времени пользователя."""
    if config.course_start is None:
        return None
    local = datetime.combine(config.course_start, time(config.delivery_hour), ZoneInfo(tz_name))
    return local.astimezone(timezone.utc)


def before_start(config: Config, tz_name: str, now: datetime | None = None) -> bool:
    start = start_at(config, tz_name)
    return start is not None and (now or datetime.now(timezone.utc)) < start


def start_pending(config: Config, today: date | None = None) -> bool:
    """Дата старта ещё не наступила (для текстов - без учёта часового пояса)."""
    if config.course_start is None:
        return False
    return (today or datetime.now(timezone.utc).date()) < config.course_start


def has_audio(day: int) -> bool:
    from bot.content import audio_path
    from bot.lesson_audio import CACHE
    return day in CACHE or audio_path(day) is not None


# --- предупреждение администратору: этап ждёт аудиопрактику ---

_waiting: dict[int, int] = {}     # день -> сколько человек ждут в этом проходе планировщика
_alerted: set[int] = set()


def note_waiting(day: int) -> None:
    _waiting[day] = _waiting.get(day, 0) + 1


async def alert_waiting(bot: Bot, config: Config) -> None:
    """Раз в процесс на каждый день: «День N ждёт аудио, K чел.». Вызывается планировщиком."""
    pending = dict(_waiting)
    _waiting.clear()
    for day, n in sorted(pending.items()):
        if day in _alerted or has_audio(day):
            continue
        _alerted.add(day)
        for admin_id in config.admin_ids:
            try:
                await bot.send_message(admin_id, texts.audio_waiting_admin(day, n))
            except Exception:
                log.exception("cannot alert admin %s", admin_id)


# --- /notify_waitlist ---

async def _waitlist(session: AsyncSession) -> list[User]:
    paid_ids = select(Purchase.user_id).where(
        Purchase.product == COURSE, Purchase.status.in_(("paid", "refund_pending")))
    return list(await session.scalars(
        select(User).where(
            User.wants_purchase.is_(True), User.is_blocked.is_(False),
            User.waitlist_notified_at.is_(None), User.id.not_in(paid_ids),
        ).order_by(User.id)
    ))


def _sales_open(config: Config) -> bool:
    return config.payment_mode in PAID_MODES and config.course_sales


@router.message(Command("notify_waitlist"))
async def cmd_notify_waitlist(message: Message, sessionmaker: async_sessionmaker, config: Config) -> None:
    if not config.is_admin(message.from_user.id):
        return
    if not _sales_open(config):
        await message.answer(texts.WAITLIST_SALES_CLOSED)
        return
    async with sessionmaker() as session:
        users = await _waitlist(session)
        cp = await course_price(session, config)
    if not users:
        await message.answer(texts.WAITLIST_EMPTY)
        return
    await message.answer(
        texts.waitlist_preview(len(users)) + "\n\n" + texts.waitlist_message(config, cp),
        reply_markup=Kb(inline_keyboard=[[Btn(text=texts.BTN_WAITLIST_SEND.format(n=len(users)),
                                              callback_data="waitlist:send")]]),
    )


@router.callback_query(F.data == "waitlist:send")
async def cb_waitlist_send(call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker,
                           config: Config) -> None:
    await call.answer()
    if not config.is_admin(call.from_user.id) or not _sales_open(config):
        return
    async with sessionmaker() as session:
        ids = [u.id for u in await _waitlist(session)]
    sent = blocked = failed = 0
    for uid in ids:
        async with sessionmaker() as session:
            user = await session.get(User, uid)
            if user is None or user.waitlist_notified_at is not None:
                continue
            # Отмечаем до отправки: повторное нажатие кнопки не пришлёт второе сообщение
            user.waitlist_notified_at = datetime.now(timezone.utc)
            await session.commit()
            cp = await course_price(session, config)
            try:
                await bot.send_message(user.tg_id, texts.waitlist_message(config, cp),
                                       reply_markup=buy_markup(cp))
                sent += 1
            except TelegramForbiddenError:
                user.is_blocked = True
                await session.commit()
                blocked += 1
            except Exception:
                log.exception("waitlist: cannot notify user %s", uid)
                failed += 1
        await asyncio.sleep(0.05)  # не больше ~20 сообщений в секунду
    await bot.send_message(call.from_user.id, texts.waitlist_done(sent, blocked, failed))


def buy_markup(cp: CoursePrice) -> Kb:
    return Kb(inline_keyboard=[[Btn(text=texts.BTN_BUY.format(price=cp.rub), callback_data="buy")]])
