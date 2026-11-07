from datetime import datetime, timedelta, timezone

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import select

from bot.config import Config
from bot.content import load_lesson
from bot.db import init_db, make_engine
from bot.models import User
from bot.delivery import deliver_day, deliver_if_due
from bot.services import (
    delivered_map, get_or_create_user, grant_purchase, has_paid, next_due_day,
)

MSK = "Europe/Moscow"
CFG = Config(
    bot_token="x", database_url="sqlite+aiosqlite://", payment_mode="test", price_rub=990,
    delivery_hour=9, default_timezone=MSK, debug_commands=True,
)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# ---------- расписание ----------

def test_day1_never_scheduled():
    assert next_due_day({}, paid=True, now=utc(2026, 1, 1, 12), tz_name=MSK, hour=9) is None


def test_unpaid_user_does_not_get_day2():
    delivered = {1: utc(2026, 1, 1, 10)}
    assert next_due_day(delivered, paid=False, now=utc(2026, 1, 5, 10), tz_name=MSK, hour=9) is None


def test_day2_waits_until_next_morning_moscow():
    delivered = {1: utc(2026, 1, 1, 10)}  # 13:00 МСК 1 января
    # 2 января 08:59 МСК = 05:59 UTC — ещё рано
    assert next_due_day(delivered, True, utc(2026, 1, 2, 5, 59), MSK, 9) is None
    # 2 января 09:00 МСК = 06:00 UTC — пора
    assert next_due_day(delivered, True, utc(2026, 1, 2, 6, 0), MSK, 9) == 2


def test_late_evening_utc_counts_as_next_local_day():
    # 22:30 UTC 1 января = 01:30 МСК 2 января → следующий день 3 января в 9:00
    delivered = {1: utc(2026, 1, 1, 22, 30)}
    assert next_due_day(delivered, True, utc(2026, 1, 2, 7), MSK, 9) is None
    assert next_due_day(delivered, True, utc(2026, 1, 3, 6), MSK, 9) == 2


def test_missed_days_are_delivered_one_at_a_time():
    delivered = {1: utc(2026, 1, 1, 10)}
    assert next_due_day(delivered, True, utc(2026, 1, 10, 3), MSK, 9) == 2


def test_nothing_after_day7():
    delivered = {d: utc(2026, 1, d, 10) for d in range(1, 8)}
    assert next_due_day(delivered, True, utc(2026, 2, 1), MSK, 9) is None


def test_naive_datetime_from_sqlite_is_utc():
    delivered = {1: datetime(2026, 1, 1, 10)}
    assert next_due_day(delivered, True, utc(2026, 1, 2, 6), MSK, 9) == 2


# ---------- контент ----------

@pytest.mark.parametrize("day", range(1, 8))
def test_lessons_load_and_fit_telegram(day):
    lesson = load_lesson(day)
    assert lesson.title
    assert any(p.is_audio for p in lesson.parts)
    assert all(len(p.text) <= 4096 for p in lesson.parts)
    text = "\n".join(p.text for p in lesson.parts)
    for section in ("Вопросы", "Задание", "Аффирмация"):
        assert f"<b>{section}" in text


# ---------- база и выдача ----------

class FakeBot:
    def __init__(self, blocked=False):
        self.sent: list[tuple[int, str]] = []
        self.blocked = blocked

    async def send_message(self, chat_id, text, **kw):
        if self.blocked:
            raise TelegramForbiddenError(SendMessage(chat_id=chat_id, text=text), "bot was blocked")
        self.sent.append((chat_id, text))

    async def send_audio(self, chat_id, audio, caption=None, **kw):
        self.sent.append((chat_id, f"[audio] {caption}"))


@pytest.fixture
async def sm():
    engine = make_engine("sqlite+aiosqlite://")
    maker = await init_db(engine)
    yield maker
    await engine.dispose()


async def _new_user(session, tg_id=111):
    user = await get_or_create_user(session, tg_id, "u", "Аня", MSK)
    await session.commit()
    return user


async def test_repeat_start_keeps_progress(sm):
    async with sm() as s:
        user = await _new_user(s)
        await deliver_day(FakeBot(), s, user, 1, CFG)
    async with sm() as s:
        again = await get_or_create_user(s, 111, "u", "Аня", MSK)
        await s.commit()
        assert again.id == user.id
        assert set(await delivered_map(s, again)) == {1}


async def test_day_is_delivered_only_once(sm):
    bot = FakeBot()
    async with sm() as s:
        user = await _new_user(s)
        assert await deliver_day(bot, s, user, 1, CFG) is True
        n = len(bot.sent)
        assert await deliver_day(bot, s, user, 1, CFG) is False
        assert len(bot.sent) == n


async def test_unpaid_user_sees_offer_after_day1(sm):
    bot = FakeBot()
    async with sm() as s:
        user = await _new_user(s)
        await deliver_day(bot, s, user, 1, CFG)
    assert "990" in bot.sent[-1][1]


async def test_repeated_payment_notification_is_idempotent(sm):
    async with sm() as s:
        user = await _new_user(s)
        assert await grant_purchase(s, user, "yookassa", "pay-1", 99000) is True
        await s.commit()
        assert await grant_purchase(s, user, "yookassa", "pay-1", 99000) is False
        await s.commit()
        assert await has_paid(s, user)


async def test_paid_user_gets_next_day_on_schedule(sm):
    bot = FakeBot()
    async with sm() as s:
        user = await _new_user(s)
        await deliver_day(bot, s, user, 1, CFG)
        await grant_purchase(s, user, "test", "test-111", 99000)
        await s.commit()
        day1_at = (await delivered_map(s, user))[1]
        later = day1_at + timedelta(days=2)
        assert await deliver_if_due(bot, s, user, CFG, now=later) == 2
        # Повторная проверка в тот же момент ничего не отправляет
        assert await deliver_if_due(bot, s, user, CFG, now=later) is None


async def test_blocked_bot_marks_user_and_does_not_record_delivery(sm):
    async with sm() as s:
        user = await _new_user(s)
        assert await deliver_day(FakeBot(blocked=True), s, user, 1, CFG) is False
    async with sm() as s:
        u = await s.scalar(select(User).where(User.tg_id == 111))
        assert u.is_blocked is True
        assert await delivered_map(s, u) == {}
    # Написала боту снова — снова считается активной
    async with sm() as s:
        u = await get_or_create_user(s, 111, "u", "Аня", MSK)
        assert u.is_blocked is False


async def test_failed_send_is_retried_later(sm):
    class Flaky(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            raise RuntimeError("network down")

    async with sm() as s:
        user = await _new_user(s)
        assert await deliver_day(Flaky(), s, user, 1, CFG) is False
        assert await delivered_map(s, user) == {}
        assert await deliver_day(FakeBot(), s, user, 1, CFG) is True
