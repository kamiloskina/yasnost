import logging

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from bot.models import Base

log = logging.getLogger(__name__)

# Колонки, добавленные после первого запуска: (таблица, колонка, SQL-определение).
# create_all не меняет существующие таблицы, поэтому добавляем их сами.
MIGRATIONS = [
    ("purchases", "product", "VARCHAR(16) NOT NULL DEFAULT 'course'"),
]


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def _migrate(sync_conn) -> None:
    insp = inspect(sync_conn)
    for table, column, ddl in MIGRATIONS:
        if not insp.has_table(table):
            continue
        if column not in {c["name"] for c in insp.get_columns(table)}:
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            log.info("migration: added %s.%s", table, column)


async def init_db(engine: AsyncEngine) -> async_sessionmaker:
    async with engine.begin() as conn:
        await conn.run_sync(_migrate)
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)
