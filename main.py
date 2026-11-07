import asyncio
import contextlib
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiohttp import web

from bot.config import load_config
from bot.db import init_db, make_engine
from bot.delivery import run_scheduler
from bot.handlers import router
from bot.payments import router as payments_router
from bot.keyboards import titles


async def start_health_server() -> web.AppRunner | None:
    """Хостинги (Amvera и др.) считают приложение запущенным, когда оно отвечает на порту.
    Бот работает через polling и порт ему не нужен, поэтому отвечаем простым «ok»."""
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", port).start()
        logging.info("health server on port %s", port)
        return runner
    except OSError as e:
        logging.warning("health server not started on port %s: %s", port, e)
        await runner.cleanup()
        return None


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

    health = await start_health_server()
    scheduler = asyncio.create_task(run_scheduler(bot, sessionmaker, config))
    logging.info("bot started, payment_mode=%s, autopost=%s", config.payment_mode, config.autopost)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler
        if health is not None:
            await health.cleanup()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
