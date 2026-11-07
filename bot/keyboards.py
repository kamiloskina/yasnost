from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Kb

from bot import texts
from bot.config import Config
from bot.content import all_titles

_titles_cache: dict[int, str] | None = None


def titles() -> dict[int, str]:
    global _titles_cache
    if _titles_cache is None:
        _titles_cache = all_titles()
    return _titles_cache


def days_list(start: int = 1) -> str:
    return "\n".join(f"{d}. {t}" for d, t in titles().items() if d >= start)


def welcome() -> Kb:
    return Kb(inline_keyboard=[[Btn(text=texts.BTN_ABOUT, callback_data="about")]])


def about() -> Kb:
    return Kb(inline_keyboard=[[Btn(text=texts.BTN_START_DAY1, callback_data="day1")]])


def offer(config: Config, user_id: int) -> Kb:
    if config.test_access(user_id):
        label = texts.BTN_BUY_TEST
    elif config.payment_mode == "stars":
        label = texts.BTN_BUY_STARS.format(stars=config.stars_price)
    else:
        label = texts.BTN_BUY.format(price=config.price_rub)
    return Kb(inline_keyboard=[
        [Btn(text=label, callback_data="buy")],
        [Btn(text=texts.BTN_MY_DAYS, callback_data="days")],
    ])


def my_days_only() -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text=texts.BTN_MY_DAYS, callback_data="days")],
        [Btn(text=texts.BTN_CHANNEL, url=texts.CHANNEL_URL)],
    ])


def days(delivered: list[int], has_tracker: bool = False) -> Kb:
    t = titles()
    rows = [[Btn(text=f"День {d}. {t[d]}", callback_data=f"open:{d}")] for d in sorted(delivered)]
    if has_tracker:
        rows.append([Btn(text=texts.BTN_TRACKER_GET, callback_data="tracker")])
    return Kb(inline_keyboard=rows)


def tracker_offer(config: Config, user_id: int) -> Kb:
    if config.test_access(user_id):
        label = texts.BTN_TRACKER_BUY_TEST
    else:
        label = texts.BTN_TRACKER_BUY.format(price=texts.tracker_price_label(config))
    return Kb(inline_keyboard=[[Btn(text=label, callback_data="buy:tracker")]])
