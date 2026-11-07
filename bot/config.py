import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
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
    payment_mode: str  # "test" | "off"
    price_rub: int
    delivery_hour: int
    default_timezone: str
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    debug_commands: bool = False


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "BOT_TOKEN не задан. Добавь его в переменные окружения (Railway → Variables) "
            "или в файл .env для локального запуска."
        )
    payment_mode = os.getenv("PAYMENT_MODE", "test").strip().lower()
    if payment_mode not in {"test", "off"}:
        raise SystemExit("PAYMENT_MODE должен быть test или off")
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
    )
