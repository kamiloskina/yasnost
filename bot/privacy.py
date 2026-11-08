"""Права субъекта персональных данных и сроки хранения (152-ФЗ, ст. 14, 20, 21).

Что делает код - то же, что обещает docs/legal/privacy.md (разделы 8-9):
- /privacy: сведения о данных (ст. 14), отзыв согласия на учёт источника (ст. 9 ч. 2),
  требование удалить данные (ст. 20 ч. 3, ст. 21). Каждое обращение получает номер и результат.
- purge_expired(): ежедневное удаление по истечении сроков хранения (ст. 5 ч. 7, ст. 21 ч. 4).
"""

from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Kb
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import legal, texts
from bot.config import Config
from bot.models import Delivery, LegalAction, PrivacyRequest, Purchase, RefundRequest, User
from bot.services import TOTAL_DAYS, as_utc, get_or_create_user

router = Router()

FREE_USER_DAYS = 365        # бесплатные пользователи: 12 месяцев с последнего действия
SOURCE_DAYS = 365           # метка источника: 12 месяцев с момента прихода
PAID_YEARS = 3              # оплаты и связанные данные: 3 года с конца года последней оплаты/возврата
REQUEST_YEARS = 3           # обращения субъекта: 3 года с даты обращения


def _now() -> datetime:
    return datetime.now(timezone.utc)


def paid_retention_until(last_event: datetime) -> datetime:
    """Конец срока хранения сведений об оплате: 31.12 года последнего события + 3 года."""
    return datetime(as_utc(last_event).year + PAID_YEARS + 1, 1, 1, tzinfo=timezone.utc)


async def _last_payment_event(session: AsyncSession, user: User) -> datetime | None:
    p = await session.scalar(select(func.max(Purchase.created_at)).where(Purchase.user_id == user.id))
    r = await session.scalar(select(func.max(RefundRequest.completed_at)).where(RefundRequest.user_id == user.id))
    events = [as_utc(x) for x in (p, r) if x is not None]
    return max(events) if events else None


async def _active_course(session: AsyncSession, user: User) -> bool:
    """Оплаченный путь ещё исполняется: оплачен и выданы не все дни."""
    paid = await session.scalar(select(func.count()).select_from(Purchase).where(
        Purchase.user_id == user.id, Purchase.product == "course", Purchase.status == "paid"))
    if not paid:
        return False
    days = await session.scalar(select(func.count()).select_from(Delivery).where(Delivery.user_id == user.id))
    return days < TOTAL_DAYS


async def delete_user_fully(session: AsyncSession, user: User) -> None:
    for model in (Delivery, LegalAction, RefundRequest, Purchase):
        await session.execute(delete(model).where(model.user_id == user.id))
    await session.delete(user)


async def destroy(session: AsyncSession, user: User) -> str:
    """Требование удалить данные. Удаляет всё, для чего нет иного основания; возвращает текст результата."""
    last = await _last_payment_event(session, user)
    if last is None:
        await delete_user_fully(session, user)
        return "Все твои данные удалены. Если вернёшься - начнёшь с чистого листа."
    user.source = None
    user.wants_purchase = False
    await session.execute(delete(LegalAction).where(
        LegalAction.user_id == user.id,
        LegalAction.action.in_((legal.ANALYTICS_CONSENT, legal.PD_CONSENT)),
    ))
    until = paid_retention_until(last).strftime("%d.%m.%Y")
    if await _active_course(session, user):
        keep = ("прогресс оплаченного пути - он ещё исполняется (договор); после его завершения "
                f"прогресс, сведения об оплатах и принятии условий хранятся до {until}")
    else:
        keep = (f"сведения об оплатах, возвратах, принятии условий и о том, какие этапы и когда "
                f"направлены, - до {until}: это нужно для налоговых целей и разрешения возможных споров")
    return f"Удалили метку источника и согласие на её учёт. Сохраняем: {keep}. Потом удалим автоматически."


async def register(session: AsyncSession, tg_id: int, kind: str) -> PrivacyRequest:
    req = PrivacyRequest(tg_id=tg_id, kind=kind)
    session.add(req)
    await session.flush()
    return req


def complete(req: PrivacyRequest, result: str) -> None:
    req.status, req.result, req.completed_at = "completed", result[:512], _now()


async def purge_expired(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """Удаление по истечении сроков (политика, раздел 8). Возвращает, сколько чего удалено."""
    now = now or _now()
    stats = {"free_users": 0, "paid_users": 0, "sources": 0, "requests": 0}
    users = list(await session.scalars(select(User)))
    for user in users:
        last_pay = await _last_payment_event(session, user)
        if last_pay is None:
            activity = as_utc(user.last_activity_at or user.created_at)
            if activity < now - timedelta(days=FREE_USER_DAYS):
                await delete_user_fully(session, user)
                stats["free_users"] += 1
                continue
        elif paid_retention_until(last_pay) <= now and not await _active_course(session, user):
            await delete_user_fully(session, user)
            stats["paid_users"] += 1
            continue
        if user.source is not None and (
            as_utc(user.created_at) < now - timedelta(days=SOURCE_DAYS)
            or not await legal.has_analytics_consent(session, user)
        ):
            user.source = None
            stats["sources"] += 1
        if user.wants_purchase and as_utc(user.last_activity_at or user.created_at) < now - timedelta(days=365):
            user.wants_purchase = False
    # Имя и username больше не собираются - очищаем сохранённые ранее (минимизация)
    await session.execute(update(User).where(or_(User.username.is_not(None), User.first_name.is_not(None)))
                          .values(username=None, first_name=None))
    old = now - timedelta(days=365 * REQUEST_YEARS)
    res = await session.execute(delete(PrivacyRequest).where(PrivacyRequest.created_at < old))
    stats["requests"] = res.rowcount or 0
    await session.commit()
    return stats


# ---------- /privacy ----------

async def _user(session: AsyncSession, obj, config: Config) -> User:
    user = await get_or_create_user(session, obj.from_user.id, None, None, config.default_timezone)
    await session.commit()
    return user


@router.message(Command("privacy"))
async def cmd_privacy(message: Message, sessionmaker: async_sessionmaker, config: Config) -> None:
    async with sessionmaker() as session:
        user = await _user(session, message, config)
        analytics = await legal.has_analytics_consent(session, user)
        has_payments = await _last_payment_event(session, user) is not None
        req = await register(session, message.from_user.id, "access")
        complete(req, "сведения показаны в чате")
        await session.commit()
    rows = []
    if analytics:
        rows.append([Btn(text=texts.BTN_ANALYTICS_WITHDRAW, callback_data="pd:analytics")])
    rows.append([Btn(text=texts.BTN_DESTROY, callback_data="pd:destroy")])
    await message.answer(texts.privacy_info(analytics, has_payments), reply_markup=Kb(inline_keyboard=rows))


@router.callback_query(F.data == "pd:analytics")
async def cb_analytics_withdraw(call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker,
                                config: Config) -> None:
    await call.answer()
    async with sessionmaker() as session:
        user = await _user(session, call, config)
        user.source = None
        await legal.record(session, user, legal.ANALYTICS_WITHDRAW)
        req = await register(session, call.from_user.id, "analytics_withdraw")
        complete(req, "метка источника удалена")
        await session.commit()
        n = req.id
    await bot.send_message(call.from_user.id, texts.ANALYTICS_WITHDRAWN.format(n=n))


@router.callback_query(F.data == "pd:destroy")
async def cb_destroy_ask(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    await bot.send_message(call.from_user.id, texts.DESTROY_CONFIRM, reply_markup=Kb(
        inline_keyboard=[[Btn(text=texts.BTN_DESTROY_CONFIRM, callback_data="pd:destroy_ok")]]))


@router.callback_query(F.data == "pd:destroy_ok")
async def cb_destroy(call: CallbackQuery, bot: Bot, sessionmaker: async_sessionmaker, config: Config) -> None:
    await call.answer()
    async with sessionmaker() as session:
        req = await register(session, call.from_user.id, "destroy")
        user = await session.scalar(select(User).where(User.tg_id == call.from_user.id))
        result = await destroy(session, user) if user else "Данных о тебе в боте нет."
        complete(req, result)
        await session.commit()
        n = req.id
    await bot.send_message(call.from_user.id, f"Обращение №{n} выполнено. {result}")
