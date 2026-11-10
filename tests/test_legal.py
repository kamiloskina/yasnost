"""Юридически значимые сценарии: бесплатный День 1, согласие, акцепт, возврат, отзыв согласия,
согласованность документов и кода."""

from dataclasses import replace
from pathlib import Path

import pytest
from aiogram.methods import AnswerPreCheckoutQuery, RefundStarPayment, SendInvoice, SendMessage
from sqlalchemy import select

from bot import legal
from bot.models import Delivery, LegalAction, Purchase, RefundRequest, User
from bot.services import delivered_map, has_paid
from tests.test_flows import ADMIN, BASE, BUYER, TESTER, Harness, all_audio, sm, texts_of  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
YK = replace(TESTER, payment_mode="yookassa", offer_url="https://t/offer",
             privacy_url="https://t/privacy", consent_url="https://t/consent")


async def actions(sm, uid=BUYER):
    async with sm() as s:
        u = await s.scalar(select(User).where(User.tg_id == uid))
        return [(a.action, a.doc_version, a.detail) for a in
                await s.scalars(select(LegalAction).where(LegalAction.user_id == u.id))]


# ---------- бесплатный День 1 и согласие ----------

async def test_start_is_not_consent_or_acceptance(sm):
    h = Harness(BASE, sm, auto_legal=False)
    await h.text("/start")
    assert await actions(sm) == []


async def test_day1_is_free_and_needs_no_consent(sm):
    h = Harness(BASE, sm, auto_legal=False)
    calls = await h.click("day1")
    assert any("День 1." in t for t in texts_of(calls))
    acts = await actions(sm)
    assert acts == [("free_accept", legal.DOC_VERSION, None)]  # согласие не навязывается
    async with sm() as s:
        assert list(await s.scalars(select(Purchase))) == []  # День 1 - без оплаты


# ---------- акцепт перед оплатой ----------

async def test_offer_acceptance_before_invoice(sm):
    h = Harness(YK, sm, auto_legal=False)
    await h.ensure_legal(BUYER, consent=True, offer=False)
    await h.click("day1")
    calls = await h.click("buy")
    assert not any(isinstance(c, SendInvoice) for c in calls)
    confirm = [c for c in calls if isinstance(c, SendMessage)][-1]
    assert "шесть платных этапов" in confirm.text and "165 ₽" in confirm.text and "https://t/offer" in confirm.text
    calls = await h.click("accept:course")
    inv = [c for c in calls if isinstance(c, SendInvoice)]
    assert inv and inv[0].prices[0].amount == 99000
    assert ("offer_accept", legal.DOC_VERSION, "course") in await actions(sm)


async def test_pre_checkout_rejected_without_acceptance(sm):
    h = Harness(replace(YK, payment_mode="stars"), sm, auto_legal=False)
    await h.text("/start")
    calls = await h.pre_checkout()
    assert [c.ok for c in calls if isinstance(c, AnswerPreCheckoutQuery)] == [False]


def test_paid_modes_require_documents(monkeypatch):
    from bot.config import load_config
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("PAYMENT_MODE", "yookassa")
    monkeypatch.setenv("YOOKASSA_PROVIDER_TOKEN", "t")
    for n in ("OFFER_URL", "PRIVACY_URL", "CONSENT_URL"):
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(SystemExit):
        load_config()


# ---------- расчёт возврата ----------

def test_stage_price_is_165_and_no_division_by_seven():
    assert BASE.price_rub == 990 and legal.stage_price(99000) == 16500
    assert legal.PAID_STAGES == (2, 3, 4, 5, 6, 7)
    assert legal.refund_amount(99000, 0) == 99000
    assert legal.refund_amount(99000, 2) == 66000
    assert legal.refund_amount(99000, 6) == 0


async def test_only_actually_sent_paid_stages_count(sm):
    from datetime import datetime, timedelta, timezone
    from bot.services import get_or_create_user
    async with sm() as s:
        u = await get_or_create_user(s, 5, None, None, "Europe/Moscow")
        await s.commit()
        old = datetime.now(timezone.utc) - timedelta(days=30)  # прошло много календарных дней
        s.add(Delivery(user_id=u.id, day=1, delivered_at=old))  # бесплатный - не считается
        s.add(Delivery(user_id=u.id, day=2, delivered_at=old))
        await s.commit()
        assert await legal.stages_provided(s, u) == 1  # время не важно - важны отправленные этапы


# ---------- процедура возврата ----------

async def test_user_return_flow_is_registered_idempotent_and_completed(sm, all_audio):
    h = Harness(TESTER, sm)
    await h.click("day1")
    await h.click("buy")
    await h.text("/next")  # направлен 1 платный этап
    calls = await h.text("/return")
    assert any("1 из 6" in t and "825 ₽" in t for t in texts_of(calls))
    async with sm() as s:
        pid = (await s.scalar(select(Purchase))).id
    calls = await h.click(f"ret:{pid}")
    assert any("Заявление №" in t and "825 ₽" in t for t in texts_of(calls))
    assert any("Заявление на возврат" in c.text for c in calls if isinstance(c, SendMessage) and c.chat_id == ADMIN)
    await h.click(f"ret:{pid}")  # повторное подтверждение
    async with sm() as s:
        reqs = list(await s.scalars(select(RefundRequest)))
        assert len(reqs) == 1 and reqs[0].amount_due == 82500 and reqs[0].stages_provided == 1
        u = await s.scalar(select(User).where(User.tg_id == BUYER))
        assert await has_paid(s, u) is False  # следующие этапы больше не выдаются
        req_id = reqs[0].id
    calls = await h.text("/next")
    assert not any("День 3." in t for t in texts_of(calls))
    calls = await h.text(f"/refund_done {req_id}", uid=ADMIN)
    assert any("зафиксирован" in t for t in texts_of(calls))
    assert any("Возврат 825 ₽ выполнен" in c.text for c in calls if isinstance(c, SendMessage) and c.chat_id == BUYER)
    calls = await h.text(f"/refund_done {req_id}", uid=ADMIN)  # двойной возврат невозможен
    assert any("уже закрыто" in t for t in texts_of(calls))
    async with sm() as s:
        assert (await s.scalar(select(Purchase))).status == "refunded"


async def test_refund_done_custom_amount(sm):
    h = Harness(TESTER, sm)
    await h.click("day1")
    await h.click("buy")
    await h.text("/next")
    await h.text(f"/refund {BUYER}", uid=ADMIN)
    async with sm() as s:
        req_id = (await s.scalar(select(RefundRequest))).id
    await h.text(f"/refund_done {req_id} 990", uid=ADMIN)  # этап к человеку не дошёл - вернули всё
    async with sm() as s:
        assert (await s.get(RefundRequest, req_id)).final_amount == 99000


async def test_stars_auto_refund_only_without_paid_stages(sm, all_audio):
    cfg = replace(TESTER, payment_mode="stars")
    h = Harness(cfg, sm)
    await h.click("day1")
    await h.paid("st-1")
    await h.text("/next")  # 1 платный этап направлен → звёзды целиком возвращать нельзя автоматически
    calls = await h.text(f"/refund {BUYER}", uid=ADMIN)
    assert not any(isinstance(c, RefundStarPayment) for c in calls)
    assert any("LEGAL_REVIEW" in c.text for c in calls if isinstance(c, SendMessage) and c.chat_id == ADMIN)


async def test_return_nothing_without_payment(sm):
    h = Harness(BASE, sm)
    calls = await h.text("/return")
    assert any("нет твоих оплат" in t for t in texts_of(calls))


# ---------- отзыв согласия ----------

# ---------- согласованность документов и интерфейса ----------

def test_documents_consistent_with_product():
    offer = (ROOT / "docs/legal/offer.md").read_text(encoding="utf-8")
    privacy = (ROOT / "docs/legal/privacy.md").read_text(encoding="utf-8")
    consent = (ROOT / "docs/legal/consent.md").read_text(encoding="utf-8")
    for doc in (offer, privacy, consent):
        assert "Аллямов" in doc and "Камил" in doc and "732771557733" in doc
        assert "141" not in doc
    assert "990" in offer and "165" in offer and "День 1" in offer and "бесплатн" in offer
    assert "3 (трёх)" not in offer and "трёх календарных" not in offer  # нет срока «3 дня» на отказ
    assert legal.DOC_VERSION == "2026-11-10"
    assert "10.11.2026" in privacy and "08.11.2026" in consent and "2026-11-10" in offer


def test_bot_texts_match_offer():
    from bot import texts
    t = texts.terms(BASE)
    assert "бесплатный" in t and "990 ₽" in t and "165 ₽" in t and "/return" in t
    b = texts.before_invoice(BASE)
    o = texts.offer_confirm(BASE, "Дни 2-7 пути", "990 ₽")
    for x in (t, b, o):
        assert "3 дней" not in x and "3 дня" not in x and "141" not in x
    assert "165 ₽" in b and "165 ₽" in o
    import inspect
    src = inspect.getsource(texts)
    assert "в течение 3 дней" not in src and "14 дней" not in src


def test_publish_versions_are_clean_and_current():
    import subprocess, sys
    subprocess.run([sys.executable, str(ROOT / "docs/legal/build_publish.py")], check=True, capture_output=True)
    for name in ("offer", "privacy", "consent"):
        src = (ROOT / f"docs/legal/{name}.md").read_text(encoding="utf-8")
        pub = (ROOT / f"docs/legal/publish/{name}.txt").read_text(encoding="utf-8")
        assert "LEGAL_REVIEW" not in pub and "**" not in pub and "`" not in pub and "|" not in pub
        assert "732771557733" in pub
        # публикационная версия соответствует исходнику (не забыли пересобрать)
        from importlib import util
        spec = util.spec_from_file_location("bp", ROOT / "docs/legal/build_publish.py")
        bp = util.module_from_spec(spec); spec.loader.exec_module(bp)
        assert bp.convert(src) == pub
    offer = (ROOT / "docs/legal/publish/offer.txt").read_text(encoding="utf-8")
    assert "990" in offer and "165" in offer and "текстовая версия" not in offer



async def test_stage_without_audio_is_not_counted_and_becomes_complete_after_audio(sm):
    from aiogram.types import Audio
    h = Harness(TESTER, sm)
    await h.click("day1")
    await h.click("buy")
    await h.text("/next")  # день 2 ушёл без записи аудиопрактики
    async with sm() as s:
        u = await s.scalar(select(User).where(User.tg_id == BUYER))
        assert await legal.stages_provided(s, u) == 0
    calls = await h.text("/return")
    assert any("0 из 6" in t and "990 ₽" in t for t in texts_of(calls))
    await h.click("aud:2", uid=ADMIN)
    await h.feed(message=h._msg(ADMIN, audio=Audio(file_id="A2", file_unique_id="a2", duration=600)))
    await h.click("aud_send:2", uid=ADMIN)
    async with sm() as s:
        u = await s.scalar(select(User).where(User.tg_id == BUYER))
        assert await legal.stages_provided(s, u) == 1  # дослали аудио - этап полный


async def test_analytics_consent_decline_records_nothing(sm):
    h = Harness(BASE, sm, auto_legal=False)
    calls = await h.text("/start reels_01")
    kb = [c for c in calls if isinstance(c, SendMessage)][-1].reply_markup
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas == ["consent:reels_01", "consent_no"]
    await h.click("consent_no")
    assert await actions(sm) == []
    async with sm() as s:
        assert (await s.scalar(select(User))).source is None


async def test_return_shows_tracker_purchase(sm):
    h = Harness(TESTER, sm)
    await h.click("buy:tracker")
    calls = await h.text("/return")
    assert any("Трекер «30 дней внимания к себе»" in t and "/paysupport" in t for t in texts_of(calls))
    calls = await h.text("/return", uid=777)
    assert any("нет твоих оплат" in t for t in texts_of(calls))
