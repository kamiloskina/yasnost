"""Автопостинг в канал по расписанию.

Посты лежат в content/channel/ и называются `ГГГГ-ММ-ДД_ЧЧММ_название.md`, например
`2026-11-10_1900_kto-ty-bez-roli.md`. Время — по DEFAULT_TIMEZONE (по умолчанию Москва).
Текст — Telegram HTML. Служебные строки (в любом месте файла):
- `@button Текст кнопки | https://ссылка` — кнопка под постом;
- `@poll Вопрос опроса` и следом 2–10 строк `@option Вариант` — анонимный опрос,
  который публикуется сразу после поста.

Бот публикует пост, когда наступило его время, но не позже чем через 6 часов
(если бот был выключен дольше, старые посты пропускаются, чтобы не завалить канал).
Каждый пост публикуется один раз: факт публикации хранится в базе.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.config import Config
from bot.content import CONTENT_DIR
from bot.models import ChannelPost

log = logging.getLogger(__name__)

CHANNEL_DIR = CONTENT_DIR / "channel"
NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})_(.+)\.md$")
MAX_LATENESS = timedelta(hours=6)


@dataclass(frozen=True)
class Post:
    slug: str
    at: datetime  # UTC
    text: str
    buttons: list[tuple[str, str]]
    poll: tuple[str, list[str]] | None = None


def parse_post(path: Path, tz_name: str) -> Post | None:
    m = NAME_RE.match(path.name)
    if not m:
        return None
    day, hh, mm, _ = m.groups()
    local = datetime.fromisoformat(f"{day}T{hh}:{mm}").replace(tzinfo=ZoneInfo(tz_name))
    lines, buttons = [], []
    poll_question, poll_options = None, []
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        if line.startswith("@button"):
            label, _, url = line[len("@button"):].partition("|")
            buttons.append((label.strip(), url.strip()))
        elif line.startswith("@poll"):
            poll_question = line[len("@poll"):].strip()
        elif line.startswith("@option"):
            poll_options.append(line[len("@option"):].strip())
        else:
            lines.append(line)
    poll = (poll_question, poll_options) if poll_question else None
    return Post(slug=path.stem, at=local.astimezone(timezone.utc), text="\n".join(lines).strip(),
                buttons=buttons, poll=poll)


def load_posts(tz_name: str, channel_dir: Path = CHANNEL_DIR) -> list[Post]:
    if not channel_dir.exists():
        return []
    posts = [parse_post(p, tz_name) for p in sorted(channel_dir.glob("*.md"))]
    return [p for p in posts if p is not None]


def due_posts(posts: list[Post], posted: set[str], now: datetime) -> list[Post]:
    return [p for p in posts if p.slug not in posted and p.at <= now <= p.at + MAX_LATENESS]


async def publish_due(
    bot: Bot, sessionmaker: async_sessionmaker, config: Config, now: datetime | None = None,
    channel_dir: Path = CHANNEL_DIR,
) -> list[str]:
    now = now or datetime.now(timezone.utc)
    posts = load_posts(config.default_timezone, channel_dir)
    published: list[str] = []
    async with sessionmaker() as session:
        posted = set(await session.scalars(select(ChannelPost.slug)))
        for post in due_posts(posts, posted, now):
            try:
                session.add(ChannelPost(slug=post.slug, posted_at=now))
                await session.flush()
            except IntegrityError:
                await session.rollback()
                continue
            markup = None
            if post.buttons:
                markup = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=label, url=url)] for label, url in post.buttons
                ])
            try:
                await bot.send_message(config.channel_id, post.text, reply_markup=markup)
            except Exception:
                await session.rollback()
                log.exception("failed to publish %s", post.slug)
                continue
            await session.commit()
            published.append(post.slug)
            log.info("published channel post %s", post.slug)
            if post.poll:
                question, options = post.poll
                try:
                    await bot.send_poll(config.channel_id, question=question, options=options,
                                        is_anonymous=True)
                except Exception:
                    log.exception("failed to publish poll for %s", post.slug)
    return published


async def autopost_status(bot: Bot, sessionmaker: async_sessionmaker, config: Config,
                          now: datetime | None = None, channel_dir: Path = CHANNEL_DIR) -> str:
    """Текст для админ-команды /autopost: включено ли, есть ли права, что выйдет дальше."""
    now = now or datetime.now(timezone.utc)
    lines = [f"<b>Автопостинг:</b> {'включён' if config.autopost else 'выключен (AUTOPOST=false)'}"]
    lines.append(f"<b>Канал:</b> {config.channel_id or 'не задан (CHANNEL_ID)'}")
    if config.channel_id:
        try:
            me = await bot.get_me()
            member = await bot.get_chat_member(config.channel_id, me.id)
            can_post = member.status == "creator" or bool(getattr(member, "can_post_messages", False))
            lines.append(
                "<b>Права бота:</b> " + ("может публиковать ✓" if can_post else
                                         f"нет права публиковать (статус: {member.status})")
            )
        except Exception as e:
            lines.append(f"<b>Права бота:</b> не удалось проверить — бот не добавлен в канал? ({e})")
    async with sessionmaker() as session:
        posted = set(await session.scalars(select(ChannelPost.slug)))
    tz = ZoneInfo(config.default_timezone)
    upcoming = [p for p in load_posts(config.default_timezone, channel_dir)
                if p.slug not in posted and p.at + MAX_LATENESS >= now]
    lines.append(f"\n<b>Опубликовано:</b> {len(posted)}. <b>В очереди:</b> {len(upcoming)}.")
    for p in upcoming[:5]:
        first = p.text.splitlines()[0] if p.text else p.slug
        first = re.sub(r"<[^>]+>", "", first)
        lines.append(f"• {p.at.astimezone(tz):%d.%m %H:%M} — {first}")
    return "\n".join(lines)
