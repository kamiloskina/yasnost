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


def offer(config: Config) -> Kb:
    label = (
        texts.BTN_BUY_TEST if config.payment_mode == "test"
        else texts.BTN_BUY.format(price=config.price_rub)
    )
    return Kb(inline_keyboard=[
        [Btn(text=label, callback_data="buy")],
        [Btn(text=texts.BTN_MY_DAYS, callback_data="days")],
    ])


def my_days_only() -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text=texts.BTN_MY_DAYS, callback_data="days")],
        [Btn(text=texts.BTN_CHANNEL, url=texts.CHANNEL_URL)],
    ])


def days(delivered: list[int]) -> Kb:
    t = titles()
    return Kb(inline_keyboard=[
        [Btn(text=f"День {d}. {t[d]}", callback_data=f"open:{d}")] for d in sorted(delivered)
    ])
