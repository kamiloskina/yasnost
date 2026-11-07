"""Персональные данные: согласие на учёт источника, права субъекта, сроки хранения, поддержка."""

from datetime import datetime, timedelta, timezone

from aiogram.methods import DeleteMessage, SendMessage
from aiogram.types import Chat, Message
from sqlalchemy import select

from bot import legal, privacy
from bot.models import Delivery, LegalAction, PrivacyRequest, Purchase, User
from bot.services import get_or_create_user, grant_purchase
from tests.test_flows import ADMIN, BASE, BUYER, TESTER, Harness, sm, texts_of  # noqa: F401

UTC = timezone.utc


async def _user(sm, tg_id=BUYER):
    async with sm() as s:
        return await s.scalar(select(User).where(User.tg_id == tg_id))


# ---------- минимизация и согласие ----------

async def test_names_are_not_stored(sm):
    h = Harness(BASE, sm)
    await h.text("/start")
    u = await _user(sm)
    assert u.username is None and u.first_name is None and u.last_activity_at is not None


async def test_analytics_consent_only_asked_with_source_and_is_optional(sm):
    h = Harness(BASE, sm, auto_legal=False)
    calls = await h.text("/start")
    assert not any("Можно я запомню" in t for t in texts_of(calls))
    calls = await h.text("/start reels_02")
    assert any("Можно я запомню" in t for t in texts_of(calls))  # пришла по метке — спрашиваем
    calls = await h.click("day1")  # без согласия — День 1 доступен
    assert any("День 1." in t for t in texts_of(calls))
    assert (await _user(sm)).source is None


async def test_welcome_links_policy(sm):
    from dataclasses import replace
    h = Harness(replace(BASE, privacy_url="https://t/privacy"), sm)
    calls = await h.text("/start")
    assert any("https://t/privacy" in t for t in texts_of(calls))


# ---------- /privacy ----------

async def test_privacy_access_request_is_registered(sm):
    h = Harness(BASE, sm)
    calls = await h.text("/privacy")
    t = texts_of(calls)[0]
    assert "Имя и ник из профиля бот не сохраняет" in t
    async with sm() as s:
        req = await s.scalar(select(PrivacyRequest))
        assert req.kind == "access" and req.status == "completed" and req.tg_id == BUYER


async def test_analytics_withdraw(sm):
    h = Harness(BASE, sm, auto_legal=False)
    await h.text("/start reels_01")
    await h.click("consent")
    assert (await _user(sm)).source == "reels_01"
    calls = await h.click("pd:analytics")
    assert any("Обращение №" in t and "метка удалена" in t for t in texts_of(calls))
    u = await _user(sm)
    assert u.source is None
    async with sm() as s:
        assert await legal.has_analytics_consent(s, u) is False


async def test_destroy_free_user_deletes_everything(sm):
    h = Harness(BASE, sm)
    await h.click("day1")
    await h.click("pd:destroy")
    calls = await h.click("pd:destroy_ok")
    assert any("Все твои данные удалены" in t for t in texts_of(calls))
    async with sm() as s:
        assert await s.scalar(select(User)) is None
        assert list(await s.scalars(select(Delivery))) == []
        assert list(await s.scalars(select(LegalAction))) == []
        req = await s.scalar(select(PrivacyRequest).where(PrivacyRequest.kind == "destroy"))
        assert req.status == "completed" and "удалены" in req.result


async def test_destroy_paid_user_keeps_legal_records_with_date(sm):
    h = Harness(TESTER, sm)
    await h.click("day1")
    await h.click("buy")
    for _ in range(6):
        await h.text("/next")
    calls = await h.click("pd:destroy_ok")
    year = datetime.now(UTC).year
    assert any(f"до 01.01.{year + 4}" in t for t in texts_of(calls))
    async with sm() as s:
        assert len(list(await s.scalars(select(Purchase)))) >= 1
        assert len(list(await s.scalars(select(Delivery)))) == 7


# ---------- сроки хранения ----------

async def test_purge_expired(sm):
    now = datetime(2026, 11, 8, tzinfo=UTC)
    async with sm() as s:
        old_free = await get_or_create_user(s, 1001, None, None, "Europe/Moscow")
        old_free.last_activity_at = now - timedelta(days=400)
        fresh_free = await get_or_create_user(s, 1002, None, None, "Europe/Moscow")
        fresh_free.last_activity_at = now - timedelta(days=10)
        fresh_free.source = "reels_01"  # метка без согласия — должна быть удалена
        fresh_free.username, fresh_free.first_name = "legacy", "Аня"  # сохранено старой версией
        old_paid = await get_or_create_user(s, 1003, None, None, "Europe/Moscow")
        recent_paid = await get_or_create_user(s, 1004, None, None, "Europe/Moscow")
        await s.flush()
        await grant_purchase(s, old_paid, "test", "p-old", 99000)
        await grant_purchase(s, recent_paid, "test", "p-new", 99000)
        await s.flush()
        for p in await s.scalars(select(Purchase)):
            p.created_at = datetime(2022, 5, 1, tzinfo=UTC) if p.provider_payment_id == "p-old" \
                else datetime(2024, 5, 1, tzinfo=UTC)
        for d in range(1, 8):
            s.add(Delivery(user_id=old_paid.id, day=d))
            s.add(Delivery(user_id=recent_paid.id, day=d))
        s.add(PrivacyRequest(tg_id=1, kind="access", created_at=now - timedelta(days=365 * 3 + 5)))
        await s.commit()
        result = await privacy.purge_expired(s, now)
    assert result == {"free_users": 1, "paid_users": 1, "sources": 1, "requests": 1}
    async with sm() as s:
        ids = set(await s.scalars(select(User.tg_id)))
        assert ids == {1002, 1004}  # 2024 → хранится до 01.01.2028
        u = await s.scalar(select(User).where(User.tg_id == 1002))
        assert u.source is None and u.username is None and u.first_name is None


def test_paid_retention_until():
    assert privacy.paid_retention_until(datetime(2026, 11, 8, tzinfo=UTC)) == datetime(2030, 1, 1, tzinfo=UTC)


# ---------- поддержка ----------

async def test_support_prompt_warns_about_health_data(sm):
    h = Harness(BASE, sm)
    calls = await h.text("/paysupport")
    assert any("не пиши сведения о здоровье" in t for t in texts_of(calls))


async def test_admin_close_deletes_support_message_and_is_not_relayed(sm):
    h = Harness(BASE, sm)
    await h.text("/paysupport")
    calls = await h.text("вопрос про оплату")
    header_text = [c for c in calls if isinstance(c, SendMessage) and c.chat_id == ADMIN][0].text
    header = Message(message_id=900, date=datetime.now(), chat=Chat(id=ADMIN, type="private"), text=header_text)
    close = Message(message_id=901, date=datetime.now(), chat=Chat(id=ADMIN, type="private"),
                    from_user=h._user(ADMIN), text="/close", reply_to_message=header)
    calls = await h.feed(message=close)
    assert sorted(c.message_id for c in calls if isinstance(c, DeleteMessage)) == [900, 901]
    assert not any(isinstance(c, SendMessage) and c.chat_id == BUYER for c in calls)
