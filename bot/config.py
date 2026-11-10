import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

PAYMENT_MODES = {"test", "off", "yookassa", "stars"}
PERSISTENT_DIR = os.getenv("PERSISTENT_DIR", "/data")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        # Amvera и подобные хостинги дают постоянную папку /data - храним базу там,
        # чтобы она не стиралась при обновлении бота. Локально - ./data/bot.db.
        if os.path.isdir(PERSISTENT_DIR) and os.access(PERSISTENT_DIR, os.W_OK):
            return f"sqlite+aiosqlite:///{PERSISTENT_DIR}/bot.db"
        return "sqlite+aiosqlite:///data/bot.db"
    # Railway отдаёт postgres://… или postgresql://…, а SQLAlchemy async нужен драйвер asyncpg.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


@dataclass(frozen=True)
class Config:
    bot_token: str
    database_url: str
    payment_mode: str  # "test" | "off" | "yookassa" | "stars"
    price_rub: int
    delivery_hour: int
    default_timezone: str
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    debug_commands: bool = False
    yookassa_provider_token: str = ""
    yookassa_send_receipt: bool = False
    stars_price: int = 0
    offer_url: str = ""
    privacy_url: str = ""
    consent_url: str = ""
    support_contact: str = ""
    channel_id: str = ""
    autopost: bool = False
    tracker_price_rub: int = 490
    tracker_stars_price: int = 0
    # Можно открыть продажи только трекера, пока не готовы аудиопрактики курса
    course_sales: bool = True

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def test_access(self, user_id: int) -> bool:
        """Тестовый доступ без денег - только администраторам и только в PAYMENT_MODE=test.
        Остальные в тестовом режиме видят «оплата скоро откроется»."""
        return self.payment_mode == "test" and self.is_admin(user_id)


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "BOT_TOKEN не задан. Добавь его в переменные окружения (Railway → Variables) "
            "или в файл .env для локального запуска."
        )
    payment_mode = os.getenv("PAYMENT_MODE", "test").strip().lower()
    if payment_mode not in PAYMENT_MODES:
        raise SystemExit("PAYMENT_MODE должен быть одним из: " + ", ".join(sorted(PAYMENT_MODES)))
    provider_token = os.getenv("YOOKASSA_PROVIDER_TOKEN", "").strip()
    if payment_mode == "yookassa" and not provider_token:
        raise SystemExit("Для PAYMENT_MODE=yookassa нужен YOOKASSA_PROVIDER_TOKEN (выдаёт @BotFather → Payments)")
    stars_price = int(os.getenv("STARS_PRICE", "0") or 0)
    if payment_mode == "stars" and stars_price <= 0:
        raise SystemExit("Для PAYMENT_MODE=stars задай STARS_PRICE - цену в звёздах")
    tracker_stars_price = int(os.getenv("TRACKER_STARS_PRICE", "0") or 0)
    if payment_mode == "stars" and tracker_stars_price <= 0:
        raise SystemExit("Для PAYMENT_MODE=stars задай TRACKER_STARS_PRICE - цену трекера в звёздах")
    if payment_mode in {"yookassa", "stars"}:
        missing = [n for n in ("OFFER_URL", "PRIVACY_URL", "CONSENT_URL") if not os.getenv(n, "").strip()]
        if missing:
            raise SystemExit(
                "Для приёма оплаты покупатель должен видеть документы до оплаты (ст. 8-10 ЗоЗПП). "
                "Задай переменные: " + ", ".join(missing)
            )
    autopost = _bool("AUTOPOST", False)
    channel_id = os.getenv("CHANNEL_ID", "").strip()
    if autopost and not channel_id:
        raise SystemExit("Для AUTOPOST=true задай CHANNEL_ID, например @onaotkrytaya")
    admin_ids = frozenset(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )
    return Config(
        bot_token=token,
        database_url=_database_url(),
        payment_mode=payment_mode,
        price_rub=int(os.getenv("PRICE_RUB", "990")),
        delivery_hour=int(os.getenv("DELIVERY_HOUR", "9")),
        default_timezone=os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow"),
        admin_ids=admin_ids,
        debug_commands=_bool("DEBUG_COMMANDS", False),
        yookassa_provider_token=provider_token,
        yookassa_send_receipt=_bool("YOOKASSA_SEND_RECEIPT", False),
        stars_price=stars_price,
        offer_url=os.getenv("OFFER_URL", "").strip(),
        privacy_url=os.getenv("PRIVACY_URL", "").strip(),
        consent_url=os.getenv("CONSENT_URL", "").strip(),
        support_contact=os.getenv("SUPPORT_CONTACT", "").strip(),
        channel_id=channel_id,
        autopost=autopost,
        tracker_price_rub=int(os.getenv("TRACKER_PRICE_RUB", "490")),
        course_sales=_bool("COURSE_SALES", True),
        tracker_stars_price=tracker_stars_price,
    )
