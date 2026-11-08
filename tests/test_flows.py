"""Сквозные сценарии через настоящий Dispatcher aiogram с подменённым Telegram API."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerPreCheckoutQuery, RefundStarPayment, SendDocument, SendInvoice, SendMessage,
)
from aiogram.types import (
    CallbackQuery, Chat, Message, PreCheckoutQuery, SuccessfulPayment, Update,
)
from aiogram.types import User as TgUser
from sqlalchemy import func, select

from bot.channel import due_posts, load_posts, publish_due
from bot.config import Config
from bot.db import init_db, make_engine
from bot.handlers import _clean_source
from bot.handlers import router as main_router
from bot.models import Purchase, User
from bot.services import delivered_map
from bot.payments import PAYLOAD, invoice_params
from bot.admin_audio import router as audio_router
from bot.admin_media import router as media_router
from bot.privacy import router as privacy_router
from bot.refunds import router as refunds_router
from bot.support import router as support_router
from bot.payments import router as pay_router

ADMIN = 1
BUYER = 42
BASE = Config(
    bot_token="1:x", database_url="sqlite+aiosqlite://", payment_mode="test", price_rub=990,
    delivery_hour=9, default_timezone="Europe/Moscow", admin_ids=frozenset({ADMIN}),
    debug_commands=True, stars_price=650, yookassa_provider_token="381764678:TEST:1",
    channel_id="@onaotkrytaya",
)
# Тестовый доступ и /next - только у админов, поэтому «покупательница» в тестах полного пути - тоже админ
TESTER = replace(BASE, admin_ids=frozenset({ADMIN, BUYER}))


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendInvoice, SendDocument)):
            return Message(message_id=1, date=datetime.now(), chat=Chat(id=1, type="private"))
        return True

    async def stream_content(self, *a, **k):
        yield b""

    async def close(self):
        pass


class Harness:
    def __init__(self, config, sm, auto_legal=True):
        self.auto_legal = auto_legal
        self.config = config
        self.session = FakeSession()
        self.bot = Bot("1:x", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.dp = Dispatcher(config=config, sessionmaker=sm)
        for r in (pay_router, audio_router, media_router, support_router, refunds_router, privacy_router,
                  main_router):
            r._parent_router = None  # роутеры модульные: отвязываем от прошлого теста
        self.dp.include_router(pay_router)
        self.dp.include_router(audio_router)
        self.dp.include_router(media_router)
        self.dp.include_router(support_router)
        self.dp.include_router(refunds_router)
        self.dp.include_router(privacy_router)
        self.dp.include_router(main_router)
        self.sm = sm
        self.n = 0

    def _user(self, uid):
        return TgUser(id=uid, is_bot=False, first_name="Аня")

    async def feed(self, **kw):
        self.n += 1
        self.session.calls.clear()
        await self.dp.feed_update(self.bot, Update(update_id=self.n, **kw))
        return self.session.calls

    async def ensure_legal(self, uid, consent=True, offer=True):
        """Согласие и акцепт, как если бы пользователь уже нажал соответствующие кнопки."""
        from bot import legal
        from bot.services import get_or_create_user
        async with self.sm() as s:
            u = await get_or_create_user(s, uid, None, "Аня", "Europe/Moscow")
            if consent and not await legal.has_pd_consent(s, u):
                await legal.record(s, u, legal.PD_CONSENT)
            if offer and not await legal.has_offer_accept(s, u):
                await legal.record(s, u, legal.OFFER_ACCEPT)
            await s.commit()

    def _msg(self, uid, **kw):
        return Message(message_id=self.n + 1, date=datetime.now(), chat=Chat(id=uid, type="private"),
                       from_user=self._user(uid), **kw)

    async def text(self, t, uid=BUYER):
        return await self.feed(message=self._msg(uid, text=t))

    async def click(self, data, uid=BUYER):
        if self.auto_legal and data in ("day1", "buy", "buy:tracker"):
            await self.ensure_legal(uid)
        m = self._msg(uid, text="x")
        return await self.feed(callback_query=CallbackQuery(
            id=str(self.n), from_user=self._user(uid), chat_instance="c", message=m, data=data))

    async def pre_checkout(self, uid=BUYER, payload=PAYLOAD):
        if self.auto_legal:
            await self.ensure_legal(uid)
        return await self.feed(pre_checkout_query=PreCheckoutQuery(
            id="pc", from_user=self._user(uid), currency="XTR", total_amount=650,
            invoice_payload=payload))

    async def paid(self, charge="ch-1", uid=BUYER, currency="XTR", amount=650, payload=PAYLOAD):
        sp = SuccessfulPayment(currency=currency, total_amount=amount, invoice_payload=payload,
                               telegram_payment_charge_id=charge, provider_payment_charge_id="p-" + charge)
        return await self.feed(message=self._msg(uid, successful_payment=sp))


def texts_of(calls):
    return [c.text for c in calls if isinstance(c, SendMessage)]


@pytest.fixture
def all_audio():
    """Имитация: аудиопрактики всех дней загружены через /audio."""
    from bot import lesson_audio
    lesson_audio.CACHE.update({d: ("audio", f"AUD{d}") for d in range(1, 8)})
    yield
    lesson_audio.CACHE.clear()


@pytest.fixture
async def sm():
    engine = make_engine("sqlite+aiosqlite://")
    maker = await init_db(engine)
    yield maker
    await engine.dispose()


# ---------- счёт ----------

def test_invoice_stars():
    p = invoice_params(replace(BASE, payment_mode="stars"))
    assert p["currency"] == "XTR" and p["prices"][0].amount == 650 and "provider_token" not in p


def test_invoice_yookassa_with_receipt():
    p = invoice_params(replace(BASE, payment_mode="yookassa", yookassa_send_receipt=True))
    assert p["currency"] == "RUB" and p["prices"][0].amount == 99000
    assert p["need_email"] and '"990.00"' in p["provider_data"]
    assert len(p["description"]) <= 255


# ---------- оплата ----------

async def test_buy_sends_invoice_in_stars_mode(sm):
    h = Harness(replace(BASE, payment_mode="stars"), sm)
    await h.click("day1")
    calls = await h.click("buy")
    assert any(isinstance(c, SendInvoice) for c in calls)


async def test_full_paid_flow_and_duplicate_payment(sm):
    h = Harness(replace(BASE, payment_mode="stars"), sm)
    await h.click("day1")
    calls = await h.pre_checkout()
    assert [c.ok for c in calls if isinstance(c, AnswerPreCheckoutQuery)] == [True]

    calls = await h.paid("ch-1")
    assert any("Доступ открыт" in t for t in texts_of(calls))
    async with sm() as s:
        assert await s.scalar(select(func.count()).select_from(Purchase)) == 1

    # Повторное уведомление о том же платеже - вторую покупку не создаёт
    calls = await h.paid("ch-1")
    assert any("уже открыт" in t for t in texts_of(calls))
    async with sm() as s:
        assert await s.scalar(select(func.count()).select_from(Purchase)) == 1

    # Уже оплатила - второй счёт Telegram не пропустит
    calls = await h.pre_checkout()
    assert [c.ok for c in calls if isinstance(c, AnswerPreCheckoutQuery)] == [False]


async def test_wrong_payload_rejected(sm):
    h = Harness(replace(BASE, payment_mode="stars"), sm)
    calls = await h.pre_checkout(payload="other")
    assert [c.ok for c in calls if isinstance(c, AnswerPreCheckoutQuery)] == [False]


async def test_pay_before_day1_gets_day1(sm):
    h = Harness(replace(BASE, payment_mode="stars"), sm)
    await h.text("/start")
    calls = await h.paid("ch-2")
    assert any("День 1." in t for t in texts_of(calls))


async def test_refund_stars_stops_access(sm):
    h = Harness(replace(BASE, payment_mode="stars"), sm)
    await h.click("day1")
    await h.paid("ch-3")
    calls = await h.text(f"/refund {BUYER}", uid=ADMIN)
    refunds = [c for c in calls if isinstance(c, RefundStarPayment)]
    assert refunds and refunds[0].telegram_payment_charge_id == "ch-3"
    async with sm() as s:
        statuses = list(await s.scalars(select(Purchase.status)))
        assert statuses == ["refunded"]
    async with sm() as s:
        u = await s.scalar(select(User).where(User.tg_id == BUYER))
        from bot.services import has_paid
        assert await has_paid(s, u) is False


async def test_refund_is_admin_only(sm):
    h = Harness(replace(BASE, payment_mode="stars"), sm)
    await h.click("day1")
    await h.paid("ch-4")
    calls = await h.text(f"/refund {BUYER}", uid=BUYER)
    assert calls == []


async def test_payment_off_records_interest(sm):
    h = Harness(replace(BASE, payment_mode="off"), sm)
    await h.click("day1")
    calls = await h.click("buy")
    assert any("скоро" in t for t in texts_of(calls))
    async with sm() as s:
        assert (await s.scalar(select(User))).wants_purchase is True


async def test_terms_and_paysupport(sm):
    h = Harness(replace(BASE, offer_url="https://example.org/offer"), sm)
    assert any("не психотерапия" in t and "оферта" in t for t in texts_of(await h.text("/terms")))
    assert texts_of(await h.text("/paysupport"))


# ---------- источники трафика ----------

def test_clean_source():
    assert _clean_source("reels_01") == "reels_01"
    assert _clean_source("bad<script>") == "badscript"
    assert _clean_source(None) is None


async def test_source_saved_once_and_shown_in_stats(sm):
    h = Harness(BASE, sm, auto_legal=False)
    calls = await h.text("/start reels_01")
    assert any("Можно я запомню" in t for t in texts_of(calls))
    async with sm() as s:
        assert (await s.scalar(select(User))).source is None  # без согласия метка не хранится
    await h.click("consent:reels_01")
    await h.text("/start channel")  # повторный старт не перезаписывает источник
    async with sm() as s:
        assert (await s.scalar(select(User))).source == "reels_01"
    calls = await h.text("/stats", uid=ADMIN)
    t = texts_of(calls)[0]
    assert "reels_01: пришли 1 → День 1: 0 → оплатили 0" in t
    assert "reels_01: 1" in t and "channel: 1" in t  # обезличенные переходы


# ---------- автопостинг ----------

def _post(dirpath: Path, name: str, body: str) -> None:
    (dirpath / name).write_text(body, encoding="utf-8")


def test_post_parsing_and_due(tmp_path):
    _post(tmp_path, "2026-11-10_1900_hello.md", "Привет\n@button В бот | https://t.me/Clarity7DaysBot")
    _post(tmp_path, "notes.md", "не пост")
    posts = load_posts("Europe/Moscow", tmp_path)
    assert len(posts) == 1
    p = posts[0]
    assert p.at == datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)
    assert p.buttons == [("В бот", "https://t.me/Clarity7DaysBot")] and p.text == "Привет"
    assert due_posts(posts, set(), p.at - timedelta(minutes=1)) == []
    assert due_posts(posts, set(), p.at) == [p]
    assert due_posts(posts, {p.slug}, p.at) == []
    assert due_posts(posts, set(), p.at + timedelta(hours=7)) == []  # слишком старый


async def test_publish_due_once(sm, tmp_path):
    _post(tmp_path, "2026-11-10_1900_hello.md", "Привет")
    h = Harness(BASE, sm)
    now = datetime(2026, 11, 10, 16, 5, tzinfo=timezone.utc)
    assert await publish_due(h.bot, sm, BASE, now, tmp_path) == ["2026-11-10_1900_hello"]
    assert [c.chat_id for c in h.session.calls] == ["@onaotkrytaya"]
    assert await publish_due(h.bot, sm, BASE, now, tmp_path) == []


async def test_autopost_status(sm, tmp_path):
    from bot.channel import autopost_status
    _post(tmp_path, "2026-11-16_1900_hello.md", "<b>Знакомство</b>\nтекст")
    h = Harness(replace(BASE, autopost=True), sm)
    text = await autopost_status(h.bot, sm, h.config, datetime(2026, 11, 10, tzinfo=timezone.utc), tmp_path)
    assert "включён" in text and "16.11 19:00 - Знакомство" in text and "В очереди:</b> 1" in text


async def test_post_with_poll(sm, tmp_path):
    from aiogram.methods import SendPoll
    _post(tmp_path, "2026-11-09_1900_q.md",
          "Вопрос\n@poll Как ты?\n@option Хорошо\n@option Устала")
    h = Harness(BASE, sm)
    now = datetime(2026, 11, 9, 16, 1, tzinfo=timezone.utc)
    assert await publish_due(h.bot, sm, BASE, now, tmp_path) == ["2026-11-09_1900_q"]
    polls = [c for c in h.session.calls if isinstance(c, SendPoll)]
    assert len(polls) == 1 and polls[0].question == "Как ты?" and len(polls[0].options) == 2
    texts = [c.text for c in h.session.calls if isinstance(c, SendMessage)]
    assert texts == ["Вопрос"]



# ---------- трекер ----------

def docs_of(calls):
    return [c for c in calls if isinstance(c, SendDocument)]


async def _finish_course(h):
    await h.click("day1")
    await h.click("buy")
    calls = []
    for _ in range(6):
        calls = await h.text("/next")
    return calls


async def test_gift_after_day7_once(sm):
    h = Harness(TESTER, sm)
    calls = await _finish_course(h)
    assert len(docs_of(calls)) == 1
    assert any("подарок" in t for t in texts_of(calls))
    async with sm() as s:
        statuses = list(await s.scalars(select(Purchase.provider).where(Purchase.product == "tracker")))
        assert statuses == ["gift"]
    # трекер есть в «Мои дни»
    calls = await h.text("/days")
    kb = [c for c in calls if isinstance(c, SendMessage)][0].reply_markup
    assert any(b.callback_data == "tracker" for row in kb.inline_keyboard for b in row)
    # /tracker просто присылает файл ещё раз, без предложения купить
    calls = await h.text("/tracker")
    assert len(docs_of(calls)) == 1 and not any("Купить" in t for t in texts_of(calls))


async def test_offer_mentions_secret_gift(sm):
    h = Harness(BASE, sm)
    calls = await h.click("day1")
    assert any("секретный подарок" in t for t in texts_of(calls))


async def test_buy_tracker_separately_test_mode(sm):
    h = Harness(TESTER, sm)
    calls = await h.text("/start tracker")
    offer = texts_of(calls)
    assert any("490 ₽" in t and "в подарок после седьмого дня" in t for t in offer)
    calls = await h.click("buy:tracker")
    assert len(docs_of(calls)) == 1
    async with sm() as s:
        from bot.models import SourceCounter
        assert (await s.scalar(select(SourceCounter))).source == "tracker"
    # курс при этом не открыт
    calls = await h.text("/next")
    assert len(docs_of(calls)) == 0


async def test_no_duplicate_gift_if_tracker_bought(sm):
    h = Harness(TESTER, sm)
    await h.click("buy:tracker")
    calls = await _finish_course(h)
    assert len(docs_of(calls)) == 0
    assert not any("Я обещала подарок" in t for t in texts_of(calls))


async def test_tracker_stars_payment(sm):
    from bot.payments import TRACKER_PAYLOAD
    cfg = replace(BASE, payment_mode="stars", tracker_stars_price=320)
    h = Harness(cfg, sm)
    calls = await h.click("buy:tracker")
    inv = [c for c in calls if isinstance(c, SendInvoice)]
    assert inv and inv[0].payload == TRACKER_PAYLOAD and inv[0].prices[0].amount == 320
    await h.ensure_legal(BUYER)
    calls = await h.feed(pre_checkout_query=PreCheckoutQuery(
        id="pc", from_user=h._user(BUYER), currency="XTR", total_amount=320,
        invoice_payload=TRACKER_PAYLOAD))
    assert [c.ok for c in calls if isinstance(c, AnswerPreCheckoutQuery)] == [True]
    calls = await h.paid("tr-1", amount=320, payload=TRACKER_PAYLOAD)
    assert len(docs_of(calls)) == 1
    async with sm() as s:
        rows = (await s.execute(select(Purchase.product, Purchase.amount))).all()
        assert rows == [("tracker", 320)]
    # повторный счёт на трекер Telegram не пропустит, а на курс - пропустит
    calls = await h.feed(pre_checkout_query=PreCheckoutQuery(
        id="pc2", from_user=h._user(BUYER), currency="XTR", total_amount=320,
        invoice_payload=TRACKER_PAYLOAD))
    assert [c.ok for c in calls if isinstance(c, AnswerPreCheckoutQuery)] == [False]
    calls = await h.pre_checkout()
    assert [c.ok for c in calls if isinstance(c, AnswerPreCheckoutQuery)] == [True]


async def test_tracker_stats(sm):
    h = Harness(TESTER, sm)
    await h.click("buy:tracker")
    calls = await h.text("/stats", uid=ADMIN)
    assert any("Купили трекер отдельно: 1" in t for t in texts_of(calls))


def test_tracker_pdf_exists():
    from bot.tracker import TRACKER_PDF
    assert TRACKER_PDF.exists() and TRACKER_PDF.stat().st_size > 100_000


async def test_migration_adds_product_column(tmp_path):
    from sqlalchemy import text
    url = f"sqlite+aiosqlite:///{tmp_path}/old.db"
    engine = make_engine(url)
    async with engine.begin() as conn:  # база «старой» версии без колонки product
        await conn.execute(text(
            "CREATE TABLE purchases (id INTEGER PRIMARY KEY, user_id INTEGER, provider VARCHAR(32), "
            "provider_payment_id VARCHAR(128) UNIQUE, amount INTEGER, currency VARCHAR(8), "
            "status VARCHAR(16), created_at DATETIME)"))
        await conn.execute(text(
            "INSERT INTO purchases (user_id, provider, provider_payment_id, amount, currency, status) "
            "VALUES (1, 'test', 'old-1', 99000, 'RUB', 'paid')"))
    maker = await init_db(engine)
    async with maker() as s:
        assert list(await s.scalars(select(Purchase.product))) == ["course"]
    await init_db(engine)  # повторный запуск ничего не ломает
    await engine.dispose()



# ---------- голосовое к посту ----------

async def test_admin_attaches_voice_and_it_goes_before_text(sm, tmp_path):
    from aiogram.methods import SendVoice
    from aiogram.types import Voice
    _post(tmp_path, "2026-11-09_1900_pochemu.md", "История\n@button В бот | https://t.me/x")
    h = Harness(BASE, sm)
    calls = await h.click("media:2026-11-09_1900_pochemu", uid=ADMIN)
    assert any("Запиши голосовое" in t for t in texts_of(calls))
    voice = Voice(file_id="VOICE1", file_unique_id="u1", duration=60)
    calls = await h.feed(message=h._msg(ADMIN, voice=voice))
    assert any("прикреплено к посту 09.11" in t for t in texts_of(calls))
    # замена записи
    await h.click("media:2026-11-09_1900_pochemu", uid=ADMIN)
    await h.feed(message=h._msg(ADMIN, voice=Voice(file_id="VOICE2", file_unique_id="u2", duration=50)))

    h.session.calls.clear()
    now = datetime(2026, 11, 9, 16, 1, tzinfo=timezone.utc)
    assert await publish_due(h.bot, sm, BASE, now, tmp_path) == ["2026-11-09_1900_pochemu"]
    kinds = [type(c).__name__ for c in h.session.calls]
    assert kinds == ["SendVoice", "SendMessage"]
    assert h.session.calls[0].voice == "VOICE2" and h.session.calls[0].chat_id == "@onaotkrytaya"


async def test_voice_from_non_admin_is_ignored(sm):
    from aiogram.types import Voice
    from bot.models import PostMedia
    h = Harness(BASE, sm)
    await h.click("media:2026-11-09_1900_pochemu", uid=BUYER)
    calls = await h.feed(message=h._msg(BUYER, voice=Voice(file_id="X", file_unique_id="x", duration=5)))
    assert calls == []
    async with sm() as s:
        assert list(await s.scalars(select(PostMedia))) == []


async def test_unmedia_removes_voice(sm, tmp_path):
    from aiogram.types import Voice
    _post(tmp_path, "2026-11-09_1900_pochemu.md", "История")
    h = Harness(BASE, sm)
    await h.click("media:2026-11-09_1900_pochemu", uid=ADMIN)
    await h.feed(message=h._msg(ADMIN, voice=Voice(file_id="V", file_unique_id="v", duration=5)))
    calls = await h.click("unmedia:2026-11-09_1900_pochemu", uid=ADMIN)
    assert any("Убрала" in t for t in texts_of(calls))
    h.session.calls.clear()
    now = datetime(2026, 11, 9, 16, 1, tzinfo=timezone.utc)
    await publish_due(h.bot, sm, BASE, now, tmp_path)
    assert [type(c).__name__ for c in h.session.calls] == ["SendMessage"]



# ---------- только администратор ----------

async def test_next_and_reset_ignored_for_regular_user(sm):
    h = Harness(BASE, sm)
    await h.click("day1")
    assert await h.text("/next") == []
    assert await h.text("/reset") == []
    async with sm() as s:
        u = await s.scalar(select(User).where(User.tg_id == BUYER))
        assert set(await delivered_map(s, u)) == {1}


async def test_test_access_only_for_admin(sm):
    h = Harness(BASE, sm)  # PAYMENT_MODE=test, BUYER не админ
    calls = await h.click("day1")
    kb = [c for c in calls if isinstance(c, SendMessage)][-1].reply_markup
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert not any("тестов" in l for l in labels) and any("990 ₽" in l for l in labels)
    calls = await h.click("buy")
    assert any("скоро" in t for t in texts_of(calls))
    calls = await h.click("buy:tracker")
    assert docs_of(calls) == [] and any("скоро" in t for t in texts_of(calls))
    async with sm() as s:
        assert list(await s.scalars(select(Purchase))) == []
        assert (await s.scalar(select(User).where(User.tg_id == BUYER))).wants_purchase is True


async def test_admin_keeps_test_access_and_next(sm):
    h = Harness(BASE, sm)
    await h.click("day1", uid=ADMIN)
    calls = await h.click("buy", uid=ADMIN)
    assert any("тестовый доступ" in t for t in texts_of(calls))
    calls = await h.text("/next", uid=ADMIN)
    assert any("День 2." in t for t in texts_of(calls))



# ---------- аудиопрактики через /audio ----------

@pytest.fixture(autouse=True)
def _clear_audio_state():
    from bot import admin_audio, admin_media, lesson_audio
    from bot import support
    lesson_audio.CACHE.clear(); admin_audio.pending.clear(); admin_media.pending.clear()
    support.pending.clear()
    yield
    lesson_audio.CACHE.clear(); admin_audio.pending.clear(); admin_media.pending.clear()


async def test_admin_uploads_lesson_audio_and_users_get_it(sm):
    from aiogram.methods import SendAudio
    from aiogram.types import Audio
    h = Harness(BASE, sm)
    calls = await h.click("aud:1", uid=ADMIN)
    assert any("День 1. Отправь" in t for t in texts_of(calls))
    calls = await h.feed(message=h._msg(ADMIN, audio=Audio(file_id="AUD1", file_unique_id="a1", duration=600)))
    assert any("загружена" in t for t in texts_of(calls))
    assert [c.audio for c in calls if isinstance(c, SendAudio)] == ["AUD1"]

    calls = await h.click("day1")  # обычная участница получает День 1
    audios = [c for c in calls if isinstance(c, SendAudio)]
    assert len(audios) == 1 and audios[0].audio == "AUD1"
    assert "Кто ты, когда никто не видит" in audios[0].caption and "наушниках" in audios[0].caption
    # сценарий практики текстом больше не приходит
    assert not any("Сядь удобно. Поставь стопы" in t for t in texts_of(calls))


async def test_lesson_audio_persists_after_restart(sm):
    from aiogram.types import Audio
    from bot import lesson_audio
    h = Harness(BASE, sm)
    await h.click("aud:2", uid=ADMIN)
    await h.feed(message=h._msg(ADMIN, audio=Audio(file_id="AUD2", file_unique_id="a2", duration=600)))
    lesson_audio.CACHE.clear()          # «перезапуск»
    await lesson_audio.load_cache(sm)
    assert lesson_audio.CACHE == {2: ("audio", "AUD2")}


async def test_audio_document_and_remove(sm):
    from aiogram.types import Document
    from bot import lesson_audio
    h = Harness(BASE, sm)
    await h.click("aud:3", uid=ADMIN)
    doc = Document(file_id="DOC3", file_unique_id="d3", file_name="day3.mp3", mime_type="audio/mpeg")
    await h.feed(message=h._msg(ADMIN, document=doc))
    assert lesson_audio.CACHE[3] == ("document", "DOC3")
    calls = await h.click("unaud:3", uid=ADMIN)
    assert any("Убрала" in t for t in texts_of(calls)) and 3 not in lesson_audio.CACHE


async def test_post_voice_still_works_with_audio_router(sm):
    from aiogram.types import Voice
    from bot import lesson_audio
    h = Harness(BASE, sm)
    await h.click("media:2026-11-09_1900_pochemu", uid=ADMIN)
    calls = await h.feed(message=h._msg(ADMIN, voice=Voice(file_id="PV", file_unique_id="pv", duration=90)))
    assert any("прикреплено к посту" in t for t in texts_of(calls))
    assert lesson_audio.CACHE == {}


async def test_audio_upload_ignored_for_non_admin(sm):
    from aiogram.types import Audio
    from bot import lesson_audio
    h = Harness(BASE, sm)
    await h.click("aud:1", uid=BUYER)
    calls = await h.feed(message=h._msg(BUYER, audio=Audio(file_id="X", file_unique_id="x", duration=5)))
    assert calls == [] and lesson_audio.CACHE == {}


async def test_refund_suggests_prorated_amount(sm, all_audio):
    cfg = replace(TESTER, payment_mode="yookassa")
    h = Harness(cfg, sm)
    await h.click("day1")
    await h.paid("yk-1", currency="RUB", amount=99000)
    await h.text("/next")
    await h.text("/next")
    async with sm() as s:
        u = await s.scalar(select(User).where(User.tg_id == BUYER))
        stages = len([d for d in await delivered_map(s, u) if d > 1])
    calls = await h.text(f"/refund {BUYER}", uid=ADMIN)
    due = 990 - 165 * stages
    admin_msgs = [t for c in calls if isinstance(c, SendMessage) and c.chat_id == ADMIN for t in [c.text]]
    assert any(f"Направлено платных этапов: {stages} из 6" in t and f"К возврату по оферте: {due} ₽" in t
               for t in admin_msgs), admin_msgs


async def test_history_shows_payments_and_delivery_times(sm):
    h = Harness(TESTER, sm)
    await h.click("day1")
    await h.click("buy")
    await h.text("/next")
    calls = await h.text(f"/history {BUYER}", uid=ADMIN)
    text = texts_of(calls)[0]
    assert "course, 990 ₽, test, статус: paid" in text and "День 1:" in text and "День 2:" in text
    assert await h.text(f"/history {BUYER}", uid=777) == []  # посторонний - тишина



# ---------- поддержка ----------

async def test_support_roundtrip(sm):
    h = Harness(BASE, sm)
    calls = await h.text("/paysupport")
    assert any("одним сообщением" in t for t in texts_of(calls))
    calls = await h.text("Оплатила, а день 2 не пришёл <срочно>")
    to_admin = [c for c in calls if isinstance(c, SendMessage) and c.chat_id == ADMIN]
    assert to_admin and "#id42" in to_admin[0].text and "&lt;срочно&gt;" in to_admin[0].text
    assert any("передала" in t for t in texts_of(calls))
    # ответ админа reply'ем
    header = Message(message_id=500, date=datetime.now(), chat=Chat(id=ADMIN, type="private"),
                     text=to_admin[0].text)
    reply = Message(message_id=501, date=datetime.now(), chat=Chat(id=ADMIN, type="private"),
                    from_user=h._user(ADMIN), text="Проверила, день 2 придёт завтра в 9:00", reply_to_message=header)
    calls = await h.feed(message=reply)
    to_user = [c for c in calls if isinstance(c, SendMessage) and c.chat_id == BUYER]
    assert to_user and "день 2 придёт завтра" in to_user[0].text
    assert any("Ответ отправлен" in t for t in texts_of(calls))


async def test_support_cancel_and_commands_not_swallowed(sm):
    h = Harness(BASE, sm)
    await h.text("/paysupport")
    calls = await h.text("/cancel")
    assert any("отменила" in t for t in texts_of(calls))
    calls = await h.text("просто текст")
    assert calls == []  # после отмены ничего не пересылается
    await h.text("/paysupport")
    calls = await h.text("/start")  # команда не уходит в поддержку
    assert any("ОНА. ОТКРЫТАЯ." in t for t in texts_of(calls))


async def test_regular_user_reply_is_not_relayed(sm):
    h = Harness(BASE, sm)
    fake = Message(message_id=1, date=datetime.now(), chat=Chat(id=BUYER, type="private"), text="#id777")
    reply = Message(message_id=2, date=datetime.now(), chat=Chat(id=BUYER, type="private"),
                    from_user=h._user(BUYER), text="hi", reply_to_message=fake)
    calls = await h.feed(message=reply)
    assert not any(isinstance(c, SendMessage) and c.chat_id == 777 for c in calls)


async def test_forget_keeps_payments(sm):
    h = Harness(TESTER, sm)
    await h.text("/start reels_01")
    await h.click("consent:reels_01")
    await h.click("day1")
    await h.click("buy")
    calls = await h.text(f"/forget {BUYER}", uid=ADMIN)
    assert any("выполнено" in t and "Сохраняем" in t for t in texts_of(calls))
    async with sm() as s:
        u = await s.scalar(select(User).where(User.tg_id == BUYER))
        assert u.source is None and u.first_name is None
        assert 1 in await delivered_map(s, u)  # путь оплачен и ещё исполняется - прогресс нужен
        assert len(list(await s.scalars(select(Purchase)))) == 1
    assert await h.text(f"/forget {BUYER}", uid=777) == []



async def test_channel_buttons_get_per_post_tag(sm, tmp_path):
    _post(tmp_path, "2026-11-09_1900_pochemu.md",
          "Текст\n@button В бот | https://t.me/Clarity7DaysBot?start=channel")
    h = Harness(BASE, sm)
    now = datetime(2026, 11, 9, 16, 1, tzinfo=timezone.utc)
    await publish_due(h.bot, sm, BASE, now, tmp_path)
    url = h.session.calls[-1].reply_markup.inline_keyboard[0][0].url
    assert url == "https://t.me/Clarity7DaysBot?start=ch1109"
    await h.text("/start ch1109")
    await h.click("consent:ch1109")
    await h.click("day1")
    calls = await h.text("/stats", uid=ADMIN)
    assert any("ch1109: пришли 1 → День 1: 1 → оплатили 0" in t for t in texts_of(calls))


async def test_practice_script_not_sent_without_audio(sm):
    h = Harness(BASE, sm)
    calls = await h.click("day1")
    t = texts_of(calls)
    assert not any("Сядь удобно. Поставь стопы" in x for x in t)
    assert any("Аудиопрактика «Кто ты, когда никто не видит»" in x and "скоро появится" in x for x in t)
    assert any("<b>Вопросы</b>" in x for x in t)  # остальной день на месте


async def test_audio_broadcast_to_day_receivers_once(sm):
    from aiogram.methods import SendAudio
    from aiogram.types import Audio
    from bot import admin_audio
    admin_audio.broadcasted.clear()
    h = Harness(BASE, sm)
    await h.click("day1")              # получила День 1 без аудио
    await h.click("day1", uid=777)     # ещё одна участница
    await h.click("aud:1", uid=ADMIN)
    calls = await h.feed(message=h._msg(ADMIN, audio=Audio(file_id="A1", file_unique_id="a", duration=600)))
    assert any("уже получили без аудио: 2 чел." in t for t in texts_of(calls))
    calls = await h.click("aud_send:1", uid=ADMIN)
    got = sorted(c.chat_id for c in calls if isinstance(c, SendAudio))
    assert got == [BUYER, 777] and any("Отправила аудио к Дню 1: 2 чел." in t for t in texts_of(calls))
    calls = await h.click("aud_send:1", uid=ADMIN)  # повторное нажатие
    assert not any(isinstance(c, SendAudio) for c in calls)
    calls = await h.click("aud_send:1", uid=BUYER)  # не админ: только снятие «часиков» с кнопки
    assert not any(isinstance(c, (SendAudio, SendMessage)) for c in calls)
