from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Нажала «Продолжить», когда оплата была закрыта: можно уведомить при открытии продаж.
    wants_purchase: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))  # test | yookassa | stars
    # Уникальный ID платежа у провайдера: повторное уведомление не создаст вторую покупку.
    provider_payment_id: Mapped[str] = mapped_column(String(128), unique=True)
    amount: Mapped[int] = mapped_column(Integer)  # в копейках / минимальных единицах
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    status: Mapped[str] = mapped_column(String(16), default="paid")  # pending | paid | refunded
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_delivery_user_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    day: Mapped[int] = mapped_column(Integer)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
