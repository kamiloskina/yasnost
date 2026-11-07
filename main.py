import asyncio
import contextlib
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.config import load_config
from bot.db import init_db, make_engine
from bot.delivery import run_scheduler
from bot.handlers import router
from bot.payments import router as payments_router
from bot.keyboards import titles


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    if config.database_url.startswith("sqlite"):
        Path("data").mkdir(exist_ok=True)
    titles()  # проверяем, что все 7 уроков на месте, до старта бота

    engine = make_engine(config.database_url)
    sessionmaker = await init_db(engine)

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="start", description="Начало и мой статус"),
        BotCommand(command="days", description="Мои дни"),
        BotCommand(command="restore", description="Восстановить доступ"),
        BotCommand(command="terms", description="Условия и возврат"),
        BotCommand(command="paysupport", description="Вопросы по оплате"),
        BotCommand(command="help", description="Помощь"),
    ])
    dp = Dispatcher(config=config, sessionmaker=sessionmaker)
    dp.include_router(payments_router)
    dp.include_router(router)

    scheduler = asyncio.create_task(run_scheduler(bot, sessionmaker, config))
    logging.info("bot started, payment_mode=%s, autopost=%s", config.payment_mode, config.autopost)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
