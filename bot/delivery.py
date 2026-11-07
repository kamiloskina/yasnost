"""Отправка уроков и фоновая ежедневная выдача."""

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import FSInputFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import keyboards, texts
from bot.config import Config
from bot.content import audio_path, load_lesson
from bot.models import Delivery, Purchase, User
from bot.services import TOTAL_DAYS, delivered_map, has_paid, next_due_day

log = logging.getLogger(__name__)


async def send_lesson(bot: Bot, chat_id: int, day: int) -> None:
    from bot.lesson_audio import send_cached

    lesson = load_lesson(day)
    for part in lesson.parts:
        if part.is_audio:
            # Аудио загружено через /audio: присылаем его с коротким вступлением вместо сценария
            title = part.text.splitlines()[0] if part.text else ""
            caption = (title + "\n\n" + texts.AUDIO_CAPTION_HINT).strip()
            if await send_cached(bot, chat_id, day, caption):
                continue
            audio = audio_path(day)
            if audio is not None:
                await bot.send_audio(chat_id, FSInputFile(audio), caption=caption)
            else:
                # Записи ещё нет: сценарий практики не отправляем — только короткое сообщение
                await bot.send_message(chat_id, texts.audio_pending(title))
            continue
        await bot.send_message(chat_id, part.text)


async def send_tail(bot: Bot, chat_id: int, day: int, paid: bool, config: Config) -> None:
    """Сообщение после урока: предложение, анонс завтрашнего дня или финал."""
    if day == TOTAL_DAYS:
        await bot.send_message(chat_id, texts.COURSE_DONE, reply_markup=keyboards.my_days_only())
    elif not paid:
        await bot.send_message(
            chat_id,
            texts.OFFER.format(days=keyboards.days_list(2), price=texts.price_label(config)),
            reply_markup=keyboards.offer(config, chat_id),
        )
    else:
        await bot.send_message(chat_id, texts.AFTER_DAY.format(hour=config.delivery_hour))


async def deliver_day(
    bot: Bot, session: AsyncSession, user: User, day: int, config: Config,
    now: datetime | None = None,
) -> bool:
    """Отправляет день один раз. Возвращает True, если урок действительно ушёл.

    Сначала резервируем запись (user, day) в базе: уникальный ключ не даст выдать день
    дважды. Если отправка не удалась, запись откатывается и выдача повторится позже.
    """
    uid, tg_id = user.id, user.tg_id
    await session.commit()  # всё, что было до выдачи (например, новый пользователь), сохраняем отдельно
    try:
        session.add(Delivery(user_id=uid, day=day, delivered_at=now or datetime.now(timezone.utc)))
        await session.flush()
    except IntegrityError:
        await session.rollback()
        await session.refresh(user)
        return False
    try:
        await send_lesson(bot, tg_id, day)
    except TelegramForbiddenError:
        await session.rollback()
        fresh = await session.get(User, uid)
        if fresh is not None:
            fresh.is_blocked = True
            await session.commit()
        log.info("user %s blocked the bot", tg_id)
        return False
    except Exception:
        await session.rollback()
        await session.refresh(user)
        log.exception("failed to deliver day %s to %s", day, tg_id)
        return False
    await session.commit()
    paid = await has_paid(session, user)
    try:
        await send_tail(bot, tg_id, day, paid, config)
        if day == TOTAL_DAYS and paid:
            from bot.tracker import gift_tracker
            await gift_tracker(bot, session, user, config)
    except Exception:
        log.exception("failed to send tail/gift for day %s to %s", day, tg_id)
    return True


async def deliver_if_due(
    bot: Bot, session: AsyncSession, user: User, config: Config, now: datetime | None = None
) -> int | None:
    now = now or datetime.now(timezone.utc)
    delivered = await delivered_map(session, user)
    paid = await has_paid(session, user)
    day = next_due_day(delivered, paid, now, user.timezone, config.delivery_hour)
    if day is not None and await deliver_day(bot, session, user, day, config, now):
        return day
    return None


async def run_scheduler(
    bot: Bot, sessionmaker: async_sessionmaker, config: Config, interval: int = 60
) -> None:
    """Раз в минуту проверяет, кому пора открыть следующий день. Состояние только в базе,
    поэтому после перезапуска ничего не теряется."""
    while True:
        try:
            async with sessionmaker() as session:
                user_ids = (await session.scalars(
                    select(User.id).where(
                        User.is_blocked.is_(False),
                        User.id.in_(select(Purchase.user_id).where(
                            Purchase.status == "paid", Purchase.product == "course"
                        )),
                    )
                )).all()
            for uid in user_ids:
                async with sessionmaker() as session:
                    user = await session.get(User, uid)
                    if user is not None:
                        await deliver_if_due(bot, session, user, config)
            if config.autopost:
                from bot.channel import publish_due
                await publish_due(bot, sessionmaker, config)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler iteration failed")
        await asyncio.sleep(interval)
