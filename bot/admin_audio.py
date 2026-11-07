"""Админ загружает аудиопрактику дня прямо в чат с ботом: /audio → день → файл."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Kb
from sqlalchemy.ext.asyncio import async_sessionmaker

import logging

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select

from bot import keyboards, texts
from bot.models import Delivery, User
from bot.config import Config
from bot.lesson_audio import CACHE, remove_audio, set_audio, send_cached
from bot.services import TOTAL_DAYS

router = Router()

log = logging.getLogger(__name__)

# {telegram_id админа: день}, для которого ждём файл
pending: dict[int, int] = {}
# Какие записи уже разосланы (день, file_id) — защита от повторного нажатия
broadcasted: set[tuple[int, str]] = set()


async def _receivers(sessionmaker: async_sessionmaker, day: int) -> list[int]:
    """Кто уже получил этот день (и не заблокировал бота)."""
    async with sessionmaker() as session:
        rows = await session.scalars(
            select(User.tg_id).join(Delivery, Delivery.user_id == User.id)
            .where(Delivery.day == day, User.is_blocked.is_(False))
        )
        return list(rows)


async def _mark_complete(sessionmaker: async_sessionmaker, tg_id: int, day: int) -> None:
    """Аудио дослано — этап у этого человека теперь полный."""
    from sqlalchemy import update
    async with sessionmaker() as session:
        uid = await session.scalar(select(User.id).where(User.tg_id == tg_id))
        await session.execute(update(Delivery).where(Delivery.user_id == uid, Delivery.day == day)
                              .values(complete=True))
        await session.commit()


@router.message(Command("audio"))
async def cmd_audio(message: Message, config: Config) -> None:
    if not config.is_admin(message.from_user.id):
        return
    t = keyboards.titles()
    rows = [[Btn(text=f"{'🎧 ' if d in CACHE else ''}День {d}. {t[d]}", callback_data=f"aud:{d}")]
            for d in range(1, TOTAL_DAYS + 1)]
    await message.answer(
        "Для какого дня загрузить аудиопрактику? 🎧 — уже загружена (можно заменить).",
        reply_markup=Kb(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("aud:"))
async def cb_audio_day(call: CallbackQuery, config: Config) -> None:
    await call.answer()
    if not config.is_admin(call.from_user.id):
        return
    from bot import admin_media
    day = int(call.data.split(":", 1)[1])
    admin_media.pending.pop(call.from_user.id, None)
    pending[call.from_user.id] = day
    await call.message.answer(
        f"День {day}. Отправь сюда аудиофайл (MP3 из GarageBand или любого приложения) "
        "или запиши голосовое. Оно заменит текст практики в уроке.\n\n"
        "Передумала — /cancel.",
        reply_markup=Kb(inline_keyboard=[[Btn(text=f"Убрать аудио у дня {day}",
                                              callback_data=f"unaud:{day}")]]),
    )


@router.callback_query(F.data.startswith("unaud:"))
async def cb_unaudio(call: CallbackQuery, sessionmaker: async_sessionmaker, config: Config) -> None:
    await call.answer()
    if not config.is_admin(call.from_user.id):
        return
    day = int(call.data.split(":", 1)[1])
    pending.pop(call.from_user.id, None)
    removed = await remove_audio(sessionmaker, day)
    await call.message.answer(f"Убрала. В дне {day} снова будет текст практики." if removed
                              else f"У дня {day} не было загруженного аудио.")


def _is_audio_document(message: Message) -> bool:
    doc = message.document
    return bool(doc and ((doc.mime_type or "").startswith("audio/")
                         or (doc.file_name or "").lower().endswith((".mp3", ".m4a", ".ogg", ".wav"))))


@router.message(F.audio | F.voice | F.document, lambda m: m.from_user.id in pending)
async def on_audio(message: Message, sessionmaker: async_sessionmaker, config: Config) -> None:
    uid = message.from_user.id
    if not config.is_admin(uid) or uid not in pending:
        return
    if message.audio:
        kind, file_id = "audio", message.audio.file_id
    elif message.voice:
        kind, file_id = "voice", message.voice.file_id
    elif _is_audio_document(message):
        kind, file_id = "document", message.document.file_id
    else:
        await message.answer("Это не похоже на аудио. Пришли MP3/M4A или запиши голосовое.")
        return
    day = pending.pop(uid)
    await set_audio(sessionmaker, day, kind, file_id)
    await message.answer(f"Аудиопрактика для дня {day} загружена ✓ Вот как её увидят участницы:")
    await send_cached(message.bot, uid, day, None)
    receivers = [r for r in await _receivers(sessionmaker, day) if r != uid]
    if receivers:
        await message.answer(
            f"День {day} уже получили без аудио: {len(receivers)} чел. Отправить им запись?",
            reply_markup=Kb(inline_keyboard=[[Btn(text=f"Отправить {len(receivers)} чел.",
                                                  callback_data=f"aud_send:{day}")]]),
        )


@router.callback_query(F.data.startswith("aud_send:"))
async def cb_audio_broadcast(call: CallbackQuery, sessionmaker: async_sessionmaker, config: Config) -> None:
    await call.answer()
    if not config.is_admin(call.from_user.id):
        return
    day = int(call.data.split(":", 1)[1])
    item = CACHE.get(day)
    if item is None:
        await call.message.answer("Для этого дня нет загруженного аудио.")
        return
    key = (day, item[1])
    if key in broadcasted:
        await call.message.answer("Эту запись уже отправляли — повторно не шлю.")
        return
    broadcasted.add(key)
    sent = 0
    for tg_id in await _receivers(sessionmaker, day):
        if tg_id == call.from_user.id:
            continue
        try:
            await send_cached(call.bot, tg_id, day, texts.audio_ready(day))
            sent += 1
            await _mark_complete(sessionmaker, tg_id, day)
        except TelegramForbiddenError:
            continue
        except Exception:
            log.exception("cannot send day %s audio to %s", day, tg_id)
    await call.message.answer(f"Отправила аудио к Дню {day}: {sent} чел. ✓")
