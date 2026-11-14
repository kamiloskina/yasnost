"""Предзаказ: цена для первых N оплат, честный счётчик мест, дата старта, рассылка ждущим."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from aiogram.methods import SendMessage
from sqlalchemy import select

from bot import legal, presale, texts
from bot.delivery import deliver_if_due
from bot.models import Purchase, User
from bot.services import get_or_create_user
from tests.test_flows import ADMIN, BASE, BUYER, TESTER, Harness, all_audio, sm, texts_of  # noqa: F401
from tests.test_yookassa import API_CFG, buttons, fake  # noqa: F401

PRESALE = replace(API_CFG, presale_price_rub=490, presale_limit=2)


async def _buy_and_pay(h, fake, uid):
    await h.click("day1", uid=uid)
    calls = list(await h.click("buy", uid=uid))  # копия: список вызовов очищается при следующем апдейте
    pid = buttons(calls)[0].url.rsplit("/", 1)[1]
    fake.pay(pid)
    await h.click("paycheck:" + pid.split("-")[1], uid=uid)
    return calls


async def test_presale_price_and_honest_counter(sm, fake, all_audio):
    h = Harness(PRESALE, sm)
    calls = await h.click("day1", uid=101)
    offer = [t for t in texts_of(calls) if "Предзаказ" in t]
    assert offer and "490 ₽ вместо 990 ₽" in offer[0] and "осталось мест: 2" in offer[0]
    assert buttons(calls)[0].text == "Продолжить путь - 490 ₽"

    calls = await _buy_and_pay(h, fake, 101)
    assert fake.created[0]["amount"] == 49000
    assert any("81,67 ₽" in t for t in texts_of(calls))  # стоимость этапа при 490 ₽

    calls = await h.click("day1", uid=102)
    assert any("осталось мест: 1" in t for t in texts_of(calls))
    await _buy_and_pay(h, fake, 102)

    # места закончились: обычная цена, без слова «предзаказ»
    calls = await h.click("day1", uid=103)
    assert not any("Предзаказ" in t for t in texts_of(calls))
    assert buttons(calls)[0].text == "Продолжить путь - 990 ₽"
    await h.click("buy", uid=103)
    assert fake.created[-1]["amount"] == 99000


async def test_test_and_gift_purchases_do_not_take_places(sm):
    async with sm() as s:
        u = await get_or_create_user(s, 5, None, "A", "Europe/Moscow")
        s.add(Purchase(user_id=u.id, provider="test", provider_payment_id="t", amount=49000, currency="RUB"))
        s.add(Purchase(user_id=u.id, provider="gift", provider_payment_id="g", amount=0, currency="RUB",
                       product="tracker"))
        await s.commit()
        cp = await presale.course_price(s, PRESALE)
    assert cp == presale.CoursePrice(490, True, 2)


async def test_presale_off_in_stars_and_by_default(sm):
    async with sm() as s:
        assert await presale.course_price(s, BASE) == presale.CoursePrice(990, False, 0)
        stars = replace(PRESALE, payment_mode="stars")
        assert (await presale.course_price(s, stars)).presale is False


def test_refund_math_for_presale_price():
    assert legal.refund_amount(49000, 0) == 49000
    assert legal.refund_amount(49000, 1) == 40833
    assert legal.refund_amount(49000, 2) == 32667  # 326,67 ₽ - как в примере оферты
    assert legal.refund_amount(49000, 6) == 0
    assert legal.fmt_amount(40833, "RUB") == "408,33 ₽" and legal.fmt_amount(49000, "RUB") == "490 ₽"
    # обычная цена - без изменений
    assert [legal.refund_amount(99000, n) for n in (0, 2, 6)] == [99000, 66000, 0]
    assert legal.stage_price(99000) == 16500


async def test_start_date_holds_paid_days(sm, all_audio):
    start = (datetime.now(timezone.utc) + timedelta(days=3)).date()
    cfg = replace(TESTER, course_start=start)
    h = Harness(cfg, sm)
    await h.click("day1")
    calls = await h.click("buy")
    assert any(texts.ru_date(start) in t and "Дни 2-7 начнут приходить" in t for t in texts_of(calls))
    starts = presale.start_at(cfg, "Europe/Moscow")
    async with sm() as s:
        user = await s.scalar(select(User).where(User.tg_id == BUYER))
        assert await deliver_if_due(h.bot, s, user, cfg, starts - timedelta(minutes=1)) is None
        assert await deliver_if_due(h.bot, s, user, cfg, starts + timedelta(minutes=1)) == 2


async def test_after_start_stage_waits_for_audio_and_admin_is_warned(sm):
    from bot import lesson_audio
    start = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    cfg = replace(TESTER, course_start=start)
    lesson_audio.CACHE[1] = ("audio", "A1")
    try:
        h = Harness(cfg, sm)
        await h.click("day1")
        await h.click("buy")
        later = datetime.now(timezone.utc) + timedelta(days=1, hours=12)
        async with sm() as s:
            user = await s.scalar(select(User).where(User.tg_id == BUYER))
            assert await deliver_if_due(h.bot, s, user, cfg, later) is None  # нет аудио Дня 2
        h.session.calls.clear()
        presale._alerted.clear()
        await presale.alert_waiting(h.bot, cfg)
        alerts = [c for c in h.session.calls if isinstance(c, SendMessage)]
        assert alerts and all("День 2 ждёт аудиопрактику" in c.text for c in alerts)
        lesson_audio.CACHE[2] = ("audio", "A2")
        async with sm() as s:
            user = await s.scalar(select(User).where(User.tg_id == BUYER))
            assert await deliver_if_due(h.bot, s, user, cfg, later) == 2
    finally:
        lesson_audio.CACHE.clear()
        presale._alerted.clear()


async def test_notify_waitlist_once(sm):
    async with sm() as s:
        for tg in (201, 202, 203, 204):
            u = await get_or_create_user(s, tg, None, "A", "Europe/Moscow")
            u.wants_purchase = tg != 204
            if tg == 202:
                s.add(Purchase(user_id=u.id, provider="yookassa", provider_payment_id="p", amount=49000,
                               currency="RUB"))
            if tg == 203:
                u.is_blocked = True
        await s.commit()
    h = Harness(PRESALE, sm)
    assert not texts_of(await h.text("/notify_waitlist", uid=BUYER))  # не админ - тишина
    calls = await h.text("/notify_waitlist", uid=ADMIN)
    assert "Ждут открытия оплаты: <b>1</b>" in texts_of(calls)[0]
    assert "осталось мест: 1" in texts_of(calls)[0]
    calls = await h.click("waitlist:send", uid=ADMIN)
    sent = [c for c in calls if isinstance(c, SendMessage) and c.chat_id == 201]
    assert len(sent) == 1 and sent[0].reply_markup.inline_keyboard[0][0].callback_data == "buy"
    assert any("Отправлено: 1" in t for t in texts_of(calls))
    calls = await h.click("waitlist:send", uid=ADMIN)  # повтор - второго сообщения нет
    assert not [c for c in calls if isinstance(c, SendMessage) and c.chat_id == 201]
    assert "Некому отправлять" in texts_of(await h.text("/notify_waitlist", uid=ADMIN))[0]


async def test_notify_waitlist_refuses_when_sales_closed(sm):
    h = Harness(replace(PRESALE, course_sales=False), sm)
    assert "закрыта" in texts_of(await h.text("/notify_waitlist", uid=ADMIN))[0]


def test_terms_mention_presale_and_full_refund_if_late():
    start = (datetime.now(timezone.utc) + timedelta(days=5)).date()
    t = texts.terms(replace(PRESALE, course_start=start))
    assert "490 ₽ вместо 990 ₽" in t and texts.ru_date(start) in t and "вернём деньги полностью" in t
    assert "Предзаказ" not in texts.terms(BASE)


async def test_buyers_lists_paid_with_profile_link(sm, fake, all_audio):
    h = Harness(PRESALE, sm)
    await _buy_and_pay(h, fake, 101)
    assert not texts_of(await h.text("/buyers", uid=BUYER))  # не админ - тишина
    out = texts_of(await h.text("/buyers", uid=ADMIN))[0]
    assert "Дни 2-7, 490 ₽" in out and 'tg://user?id=101' in out
