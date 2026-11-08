"""Поддержка через бота: человек пишет вопрос, бот пересылает его администраторам.
Администратор отвечает ответом (reply) на это сообщение - бот отправляет ответ человеку.
Личный аккаунт администратора человек не видит."""

import logging
import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import texts
from bot.config import Config

log = logging.getLogger(__name__)
router = Router()

# Кто сейчас пишет обращение
pending: set[int] = set()
ID_RE = re.compile(r"#id(\d+)")


@router.message(Command("paysupport", "support"))
async def cmd_support(message: Message, config: Config) -> None:
    pending.add(message.from_user.id)
    await message.answer(texts.SUPPORT_PROMPT)


@router.message(Command("cancel"), lambda m: m.from_user.id in pending)
async def cmd_cancel_support(message: Message) -> None:
    pending.discard(message.from_user.id)
    await message.answer("Хорошо, отменила.")


def _is_command(message: Message) -> bool:
    return bool(message.text and message.text.startswith("/"))


@router.message(lambda m: m.from_user.id in pending and not _is_command(m))
async def on_support_message(message: Message, bot: Bot, config: Config) -> None:
    uid = message.from_user.id
    pending.discard(uid)
    if not config.admin_ids:
        await message.answer(texts.SUPPORT_NO_ADMIN)
        return
    who = escape(message.from_user.full_name) + (
        f" (@{message.from_user.username})" if message.from_user.username else "")
    body = escape(message.text or message.caption or "(вложение - ниже)")
    header = (
        f"📩 Обращение #id{uid}\nОт: {who}\n\n{body}\n\n"
        "<i>Ответь на это сообщение (reply) - ответ уйдёт человеку от имени бота. "
        "Когда вопрос решён - ответь на него командой /close, и бот удалит обращение из этого чата.</i>"
    )
    delivered = False
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, header)
            if not message.text:
                await bot.copy_message(admin_id, message.chat.id, message.message_id)
            delivered = True
        except Exception:
            log.exception("cannot forward support message to admin %s", admin_id)
    await message.answer(texts.SUPPORT_SENT if delivered else texts.SUPPORT_NO_ADMIN)


def _support_target(message: Message) -> int | None:
    reply = message.reply_to_message
    if reply is None or not reply.text:
        return None
    m = ID_RE.search(reply.text)
    return int(m.group(1)) if m else None


@router.message(Command("close"), F.reply_to_message, lambda m: _support_target(m) is not None)
async def on_admin_close(message: Message, bot: Bot, config: Config) -> None:
    """Админ: /close ответом на обращение - удалить его из чата (срок хранения - политика, разд. 8)."""
    if not config.is_admin(message.from_user.id):
        return
    ok = True
    for mid in (message.reply_to_message.message_id, message.message_id):
        try:
            await bot.delete_message(message.chat.id, mid)
        except Exception:
            ok = False
    if not ok:
        await message.answer("Не все сообщения удалось удалить (Telegram ограничивает удаление старых "
                             "сообщений). Удали обращение вручную: долгое нажатие → «Удалить».")


@router.message(F.reply_to_message, lambda m: _support_target(m) is not None)
async def on_admin_reply(message: Message, bot: Bot, config: Config) -> None:
    if not config.is_admin(message.from_user.id) or (message.text or "").startswith("/"):
        return
    target = _support_target(message)
    try:
        if message.text:
            await bot.send_message(target, texts.SUPPORT_REPLY_PREFIX + message.text, parse_mode=None)
        else:
            await bot.send_message(target, texts.SUPPORT_REPLY_PREFIX.strip())
            await bot.copy_message(target, message.chat.id, message.message_id)
    except Exception as e:
        await message.answer(f"Не получилось отправить: {e}")
        return
    await message.answer("Ответ отправлен ✓")
