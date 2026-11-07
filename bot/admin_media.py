"""Админ прикрепляет голосовое или кружок к посту канала прямо из чата с ботом.

/voice → бот показывает ближайшие посты → админ выбирает → записывает голосовое
(или кружок) и отправляет боту → оно выйдет в канал вместе с постом.
"""

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Kb
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.channel import attach_media, detach_media, post_title, upcoming_posts
from bot.config import Config
from bot.models import ChannelPost, PostMedia

router = Router()

# Кто из админов сейчас записывает голосовое и к какому посту: {telegram_id: slug}
pending: dict[int, str] = {}


def _date(slug: str) -> str:
    return f"{slug[8:10]}.{slug[5:7]}"


@router.message(Command("voice"))
async def cmd_voice(message: Message, sessionmaker: async_sessionmaker, config: Config) -> None:
    if message.from_user.id not in config.admin_ids:
        return
    async with sessionmaker() as session:
        posted = set(await session.scalars(select(ChannelPost.slug)))
        with_media = set(await session.scalars(select(PostMedia.slug)))
    posts = upcoming_posts(posted, datetime.now(timezone.utc), config.default_timezone)[:10]
    if not posts:
        await message.answer("В очереди нет постов.")
        return
    rows = [[Btn(text=f"{_date(p.slug)} — {post_title(p)[:40]}{' 🎙' if p.slug in with_media else ''}",
                 callback_data=f"media:{p.slug}")] for p in posts]
    await message.answer(
        "К какому посту прикрепить голосовое или кружок? 🎙 — уже прикреплено (можно заменить).",
        reply_markup=Kb(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("media:"))
async def cb_media(call: CallbackQuery, config: Config) -> None:
    await call.answer()
    if call.from_user.id not in config.admin_ids:
        return
    from bot import admin_audio
    slug = call.data.split(":", 1)[1]
    admin_audio.pending.pop(call.from_user.id, None)
    pending[call.from_user.id] = slug
    await call.message.answer(
        f"Пост {_date(slug)}. Запиши голосовое (или кружок) и отправь сюда — "
        "оно выйдет в канал вместе с постом, перед текстом.\n\n"
        "Передумала — /cancel. Убрать уже прикреплённое — кнопка ниже.",
        reply_markup=Kb(inline_keyboard=[[Btn(text="Убрать голосовое у этого поста",
                                              callback_data=f"unmedia:{slug}")]]),
    )


@router.callback_query(F.data.startswith("unmedia:"))
async def cb_unmedia(call: CallbackQuery, sessionmaker: async_sessionmaker, config: Config) -> None:
    await call.answer()
    if call.from_user.id not in config.admin_ids:
        return
    slug = call.data.split(":", 1)[1]
    pending.pop(call.from_user.id, None)
    removed = await detach_media(sessionmaker, slug)
    await call.message.answer("Убрала. Пост выйдет только текстом." if removed
                              else "У этого поста не было голосового.")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, config: Config) -> None:
    from bot import admin_audio
    uid = message.from_user.id
    if uid not in config.admin_ids:
        return
    cancelled = pending.pop(uid, None) or admin_audio.pending.pop(uid, None)
    if cancelled:
        await message.answer("Отменила.")


@router.message(F.voice | F.video_note, lambda m: m.from_user.id in pending)
async def on_media(message: Message, sessionmaker: async_sessionmaker, config: Config) -> None:
    uid = message.from_user.id
    if uid not in config.admin_ids or uid not in pending:
        return
    slug = pending.pop(uid)
    if message.voice:
        kind, file_id, what = "voice", message.voice.file_id, "Голосовое"
    else:
        kind, file_id, what = "video_note", message.video_note.file_id, "Кружок"
    await attach_media(sessionmaker, slug, kind, file_id)
    await message.answer(
        f"{what} прикреплено к посту {_date(slug)} ✓\n"
        "Оно выйдет в канал вместе с постом. Проверить очередь — /autopost."
    )
