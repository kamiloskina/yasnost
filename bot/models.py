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
    # Откуда пришла: параметр ссылки t.me/Clarity7DaysBot?start=<source> (reels, channel, …)
    source: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Продукт: course — дни 2–7 пути, tracker — трекер на 30 дней
    product: Mapped[str] = mapped_column(String(16), default="course", server_default="course")
    provider: Mapped[str] = mapped_column(String(32))  # test | yookassa | stars | gift
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


class ChannelPost(Base):
    """Опубликованные автопостингом посты канала (чтобы не публиковать дважды)."""
    __tablename__ = "channel_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PostMedia(Base):
    """Голосовое или кружок, прикреплённые к посту канала (присылаются админом боту)."""
    __tablename__ = "post_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # voice | video_note
    file_id: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LessonAudio(Base):
    """Аудиопрактика дня, загруженная админом через бота (/audio)."""
    __tablename__ = "lesson_audio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[int] = mapped_column(Integer, unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # audio | voice | document
    file_id: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
