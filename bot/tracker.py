"""Трекер на 30 дней: подарок после 7-го дня пути и отдельная покупка."""

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Config
from bot.models import User
from bot.services import TRACKER, grant_purchase, has_paid

log = logging.getLogger(__name__)

TRACKER_PDF = Path(__file__).resolve().parent.parent / "products" / "tracker" / "tracker.pdf"
TRACKER_FILENAME = "Трекер_30_дней.pdf"


async def send_tracker(bot: Bot, chat_id: int, caption: str = texts.TRACKER_CAPTION) -> None:
    await bot.send_document(chat_id, FSInputFile(TRACKER_PDF, filename=TRACKER_FILENAME), caption=caption)


async def gift_tracker(bot: Bot, session: AsyncSession, user: User, config: Config) -> bool:
    """Дарит трекер после 7-го дня оплаченного пути. Один раз на человека.

    Если трекер уже был куплен отдельно, подарок не дублируется.
    """
    already = await has_paid(session, user, TRACKER)
    created = await grant_purchase(
        session, user, provider="gift", provider_payment_id=f"gift-tracker-{user.id}",
        amount=0, product=TRACKER,
    )
    await session.commit()
    if not created or already:
        return False
    await bot.send_message(
        user.tg_id, texts.GIFT_REVEAL.format(price=f"{config.tracker_price_rub} ₽"),
    )
    await send_tracker(bot, user.tg_id)
    return True


async def offer_tracker(bot: Bot, session: AsyncSession, user: User, config: Config) -> None:
    """Показывает трекер: если уже есть — присылает файл, иначе — предложение купить."""
    if await has_paid(session, user, TRACKER):
        await bot.send_message(user.tg_id, texts.TRACKER_ALREADY)
        await send_tracker(bot, user.tg_id)
        return
    text = texts.TRACKER_OFFER.format(price=texts.tracker_price_label(config))
    if not await has_paid(session, user):  # курс не куплен — намекнём на подарок
        text += "\n\n" + texts.TRACKER_HINT_COURSE
    await bot.send_message(user.tg_id, text, reply_markup=keyboards.tracker_offer(config))
