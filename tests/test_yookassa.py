"""Оплата на странице ЮKassa (PAYMENT_MODE=yookassa_api) с поддельным клиентом: без сети и денег."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.methods import SendDocument, SendMessage
from sqlalchemy import select

from bot import yookassa
from bot.models import PendingPayment, Purchase
from tests.test_flows import ADMIN, BASE, BUYER, Harness, all_audio, sm, texts_of  # noqa: F401

API_CFG = replace(BASE, payment_mode="yookassa_api", yookassa_shop_id="1", yookassa_secret_key="k")


class FakeYooKassa:
    def __init__(self):
        self.created = []
        self.status = {}  # payment_id -> dict
        self.fail_create = False

    async def create_payment(self, amount_kop, description, order_id, return_url):
        if self.fail_create:
            raise yookassa.YooKassaError("500")
        pid = f"pay-{order_id}"
        self.created.append(dict(amount=amount_kop, description=description, order=order_id, return_url=return_url))
        self.status[pid] = {"id": pid, "status": "pending", "paid": False,
                            "amount": {"value": f"{amount_kop / 100:.2f}", "currency": "RUB"},
                            "metadata": {"order": str(order_id)}}
        return {"id": pid, "confirmation": {"confirmation_url": f"https://yoomoney.example/{pid}"}}

    async def get_payment(self, payment_id):
        return self.status[payment_id]

    def pay(self, pid):
        self.status[pid].update(status="succeeded", paid=True)


@pytest.fixture
def fake(monkeypatch):
    f = FakeYooKassa()
    monkeypatch.setattr(yookassa, "make_client", lambda config: f)
    return f


def buttons(calls):
    kb = [c for c in calls if isinstance(c, SendMessage) and c.reply_markup][-1].reply_markup
    return [b for row in kb.inline_keyboard for b in row]


async def test_course_payment_link_and_auto_access(sm, fake):
    h = Harness(API_CFG, sm)
    await h.click("day1")
    calls = await h.click("buy")
    btns = buttons(calls)
    assert btns[0].url == "https://yoomoney.example/pay-1" and btns[1].callback_data == "paycheck:1"
    req = fake.created[0]
    assert req["amount"] == 99000 and "дни 2-7" in req["description"] and req["order"] == 1
    assert str(BUYER) not in str(req)  # Telegram ID в ЮKassa не уходит
    calls = await h.click("paycheck:1")
    assert any("ещё не поступила" in t for t in texts_of(calls))
    fake.pay("pay-1")
    calls = await h.click("paycheck:1")
    assert any("Доступ открыт" in t for t in texts_of(calls))
    calls = await h.click("paycheck:1")  # повтор - ничего не выдаёт второй раз
    assert not any("Доступ открыт" in t for t in texts_of(calls))
    async with sm() as s:
        p = list(await s.scalars(select(Purchase)))
        assert len(p) == 1 and p[0].provider == "yookassa" and p[0].provider_payment_id == "pay-1"


async def test_poller_grants_once_and_expires_old(sm, fake):
    h = Harness(API_CFG, sm)
    await h.click("day1")
    await h.click("buy")
    fake.pay("pay-1")
    await yookassa.poll_pending(h.bot, sm, API_CFG)
    await yookassa.poll_pending(h.bot, sm, API_CFG)
    async with sm() as s:
        assert len(list(await s.scalars(select(Purchase)))) == 1
    # старый неоплаченный платёж закрывается
    async with sm() as s:
        s.add(PendingPayment(user_id=1, product="tracker", amount=49000, payment_id="old",
                             status="pending", created_at=datetime.now(timezone.utc) - timedelta(days=2)))
        await s.commit()
    await yookassa.poll_pending(h.bot, sm, API_CFG)
    async with sm() as s:
        assert (await s.scalar(select(PendingPayment).where(PendingPayment.payment_id == "old"))).status == "canceled"


async def test_amount_mismatch_is_rejected(sm, fake):
    h = Harness(API_CFG, sm)
    await h.click("day1")
    await h.click("buy")
    fake.pay("pay-1")
    fake.status["pay-1"]["amount"]["value"] = "1.00"
    calls = await h.click("paycheck:1")
    assert any("Не получилось проверить" in t for t in texts_of(calls))
    async with sm() as s:
        assert list(await s.scalars(select(Purchase))) == []


async def test_tracker_via_api_sends_pdf(sm, fake):
    h = Harness(API_CFG, sm)
    await h.click("buy:tracker")
    assert fake.created[0]["amount"] == 49000
    fake.pay("pay-1")
    calls = await h.click("paycheck:1")
    assert len([c for c in calls if isinstance(c, SendDocument)]) == 1


async def test_link_reused_and_foreign_check_ignored(sm, fake):
    h = Harness(API_CFG, sm)
    await h.click("day1")
    await h.click("buy")
    await h.click("buy")
    assert len(fake.created) == 1  # свежая ссылка используется повторно
    calls = await h.click("paycheck:1", uid=777)
    assert not any(isinstance(c, SendMessage) for c in calls)


async def test_create_failure_and_course_sales_off(sm, fake):
    fake.fail_create = True
    h = Harness(API_CFG, sm)
    await h.click("day1")
    calls = await h.click("buy")
    assert any("Не получилось создать платёж" in t for t in texts_of(calls))
    fake.fail_create = False
    h2 = Harness(replace(API_CFG, course_sales=False), sm)
    calls = await h2.click("buy", uid=555)
    assert any("скоро" in t for t in texts_of(calls)) and len(fake.created) == 0


def test_api_mode_requires_keys(monkeypatch):
    from bot.config import load_config
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("PAYMENT_MODE", "yookassa_api")
    monkeypatch.delenv("YOOKASSA_SHOP_ID", raising=False)
    monkeypatch.delenv("YOOKASSA_SECRET_KEY", raising=False)
    with pytest.raises(SystemExit):
        load_config()


async def test_real_client_request_shape(monkeypatch):
    """Настоящий YooKassaClient против локального сервера: авторизация, ключ идемпотентности, тело."""
    import base64
    from aiohttp import web
    seen = {}

    async def payments(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["idem"] = request.headers.get("Idempotence-Key")
        seen["body"] = await request.json()
        return web.json_response({"id": "p1", "confirmation": {"confirmation_url": "https://pay/p1"}})

    async def payment(request):
        return web.json_response({"id": request.match_info["pid"], "status": "pending"})

    app = web.Application()
    app.router.add_post("/v3/payments", payments)
    app.router.add_get("/v3/payments/{pid}", payment)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18765)
    await site.start()
    monkeypatch.setattr(yookassa, "API", "http://127.0.0.1:18765/v3")
    try:
        c = yookassa.YooKassaClient("shop", "secret")
        r = await c.create_payment(99000, "Курс", 7, "https://t.me/x")
        assert r["confirmation"]["confirmation_url"] == "https://pay/p1"
        assert seen["auth"] == "Basic " + base64.b64encode(b"shop:secret").decode()
        assert seen["idem"] == "order-7"
        assert seen["body"]["amount"] == {"value": "990.00", "currency": "RUB"}
        assert seen["body"]["capture"] is True and seen["body"]["metadata"] == {"order": "7"}
        assert (await c.get_payment("p1"))["status"] == "pending"
    finally:
        await runner.cleanup()
