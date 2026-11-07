"""Бизнес-логика: пользователи, доступ, расписание. Без зависимостей от Telegram."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Delivery, Purchase, User

TOTAL_DAYS = 7
FREE_DAYS = 1  # День 1 бесплатно, дни 2–7 после оплаты


def as_utc(dt: datetime) -> datetime:
    # SQLite возвращает «наивные» datetime: считаем, что они в UTC.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


async def get_or_create_user(
    session: AsyncSession, tg_id: int, username: str | None, first_name: str | None, default_tz: str,
    source: str | None = None,
) -> User:
    """Имя и username из профиля не сохраняются: для работы бота они не нужны (минимизация,
    ч. 5 ст. 5 152-ФЗ). Метка источника сохраняется только с согласия (см. bot/handlers.py),
    поэтому параметр source используется лишь вызывающим кодом, получившим согласие."""
    now = datetime.now(timezone.utc)
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None:
        user = User(tg_id=tg_id, timezone=default_tz, source=source, last_activity_at=now)
        session.add(user)
        await session.flush()
    else:
        user.is_blocked = False  # написала боту, значит, не заблокировала
        user.last_activity_at = now
    return user


COURSE = "course"
TRACKER = "tracker"


async def has_paid(session: AsyncSession, user: User, product: str = COURSE) -> bool:
    """Есть ли у пользователя этот продукт (оплачен или подарен)."""
    count = await session.scalar(
        select(func.count()).select_from(Purchase).where(
            Purchase.user_id == user.id, Purchase.status == "paid", Purchase.product == product
        )
    )
    return bool(count)


async def grant_purchase(
    session: AsyncSession, user: User, provider: str, provider_payment_id: str, amount: int,
    currency: str = "RUB", product: str = COURSE,
) -> bool:
    """Записывает оплату. Возвращает False, если этот платёж уже был учтён (повторное уведомление)."""
    existing = await session.scalar(
        select(Purchase).where(Purchase.provider_payment_id == provider_payment_id)
    )
    if existing is not None:
        return False
    try:
        session.add(Purchase(
            user_id=user.id, provider=provider, provider_payment_id=provider_payment_id,
            amount=amount, currency=currency, status="paid", product=product,
        ))
        await session.flush()
    except IntegrityError:
        # Параллельное уведомление о том же платеже успело раньше
        await session.rollback()
        return False
    return True


async def paid_purchases(session: AsyncSession, user: User, product: str = COURSE) -> list[Purchase]:
    return list(await session.scalars(
        select(Purchase).where(
            Purchase.user_id == user.id, Purchase.status == "paid", Purchase.product == product
        )
    ))


async def refund_user(session: AsyncSession, user: User, product: str = COURSE) -> list[Purchase]:
    """Помечает оплаты продукта как возвращённые (для курса — выдача дней 2–7 прекращается)."""
    purchases = await paid_purchases(session, user, product)
    for p in purchases:
        p.status = "refunded"
    return purchases


async def delivered_map(session: AsyncSession, user: User) -> dict[int, datetime]:
    rows = await session.execute(
        select(Delivery.day, Delivery.delivered_at).where(Delivery.user_id == user.id)
    )
    return {day: as_utc(at) for day, at in rows.all()}


def due_time_for_next_day(prev_delivered_at: datetime, tz_name: str, hour: int) -> datetime:
    """Следующий день открывается в `hour`:00 по местному времени на следующий календарный день."""
    tz = ZoneInfo(tz_name)
    prev_local_date: date = as_utc(prev_delivered_at).astimezone(tz).date()
    return datetime.combine(prev_local_date + timedelta(days=1), time(hour), tzinfo=tz)


def next_due_day(
    delivered: dict[int, datetime], paid: bool, now: datetime, tz_name: str, hour: int
) -> int | None:
    """Какой день пора отправить сейчас. None — ничего не нужно.

    День 1 выдаётся только по кнопке, а не по расписанию.
    """
    if not delivered:
        return None
    last = max(delivered)
    nxt = last + 1
    if nxt > TOTAL_DAYS:
        return None
    if nxt > FREE_DAYS and not paid:
        return None
    if as_utc(now) >= due_time_for_next_day(delivered[last], tz_name, hour):
        return nxt
    return None


async def reset_user(session: AsyncSession, user: User) -> None:
    await session.execute(delete(Delivery).where(Delivery.user_id == user.id))
    await session.execute(delete(Purchase).where(Purchase.user_id == user.id))
    user.wants_purchase = False


async def stats(session: AsyncSession) -> dict[str, int]:
    async def count(stmt) -> int:
        return int(await session.scalar(stmt) or 0)

    return {
        "users": await count(select(func.count()).select_from(User)),
        "started_day1": await count(
            select(func.count(func.distinct(Delivery.user_id))).where(Delivery.day == 1)
        ),
        "wants_purchase": await count(
            select(func.count()).select_from(User).where(User.wants_purchase.is_(True))
        ),
        "paid": await count(
            select(func.count(func.distinct(Purchase.user_id))).where(
                Purchase.status == "paid", Purchase.product == COURSE,
                Purchase.provider != "gift",
            )
        ),
        "tracker_paid": await count(
            select(func.count(func.distinct(Purchase.user_id))).where(
                Purchase.status == "paid", Purchase.product == TRACKER,
                Purchase.provider != "gift",
            )
        ),
        "tracker_gift": await count(
            select(func.count(func.distinct(Purchase.user_id))).where(
                Purchase.status == "paid", Purchase.product == TRACKER,
                Purchase.provider == "gift",
            )
        ),
        "completed": await count(
            select(func.count(func.distinct(Delivery.user_id))).where(Delivery.day == TOTAL_DAYS)
        ),
        "blocked": await count(
            select(func.count()).select_from(User).where(User.is_blocked.is_(True))
        ),
    }


async def stats_by_source(session: AsyncSession) -> list[tuple[str, int, int, int]]:
    """(источник, сколько пришло, сколько открыли День 1, сколько оплатили путь)"""
    paid_users = select(Purchase.user_id).where(
        Purchase.status == "paid", Purchase.product == COURSE, Purchase.provider != "gift"
    )
    day1_users = select(Delivery.user_id).where(Delivery.day == 1)
    src = func.coalesce(User.source, "—")
    rows = await session.execute(
        select(
            src,
            func.count(User.id),
            func.count(User.id).filter(User.id.in_(day1_users)),
            func.count(User.id).filter(User.id.in_(paid_users)),
        ).group_by(src).order_by(func.count(User.id).desc())
    )
    return [(s_, int(n), int(d), int(p)) for s_, n, d, p in rows.all()]
