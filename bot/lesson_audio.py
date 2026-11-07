"""Аудиопрактики дней, загруженные через бота.

Хранятся в базе (file_id Telegram) и кэшируются в памяти, чтобы выдача урока не ходила в базу.
"""

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.models import LessonAudio

# {день: (вид, file_id)}
CACHE: dict[int, tuple[str, str]] = {}


async def load_cache(sessionmaker: async_sessionmaker) -> None:
    async with sessionmaker() as session:
        rows = await session.scalars(select(LessonAudio))
        CACHE.clear()
        CACHE.update({r.day: (r.kind, r.file_id) for r in rows})


async def set_audio(sessionmaker: async_sessionmaker, day: int, kind: str, file_id: str) -> None:
    async with sessionmaker() as session:
        row = await session.scalar(select(LessonAudio).where(LessonAudio.day == day))
        if row is None:
            session.add(LessonAudio(day=day, kind=kind, file_id=file_id))
        else:
            row.kind, row.file_id = kind, file_id
        await session.commit()
    CACHE[day] = (kind, file_id)


async def remove_audio(sessionmaker: async_sessionmaker, day: int) -> bool:
    async with sessionmaker() as session:
        row = await session.scalar(select(LessonAudio).where(LessonAudio.day == day))
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
    CACHE.pop(day, None)
    return True


async def send_cached(bot: Bot, chat_id: int, day: int, caption: str | None) -> bool:
    """Отправляет загруженную аудиопрактику дня. False — если её нет."""
    item = CACHE.get(day)
    if item is None:
        return False
    kind, file_id = item
    if kind == "voice":
        await bot.send_voice(chat_id, file_id, caption=caption)
    elif kind == "document":
        await bot.send_document(chat_id, file_id, caption=caption)
    else:
        await bot.send_audio(chat_id, file_id, caption=caption)
    return True
