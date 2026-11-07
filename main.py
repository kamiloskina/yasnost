import asyncio
import contextlib
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat
from aiohttp import web

from bot.config import load_config
from bot.db import init_db, make_engine
from bot.delivery import run_scheduler
from bot.handlers import router
from bot.admin_audio import router as audio_router
from bot.admin_media import router as media_router
from bot.support import router as support_router
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
    from bot.lesson_audio import load_cache
    await load_cache(sessionmaker)

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # Проверка токена и сброс вебхука: если старая версия бота принимала сообщения через вебхук,
    # Telegram не отдаёт их в polling, и бот молчит.
    me = await bot.get_me()
    info = await bot.get_webhook_info()
    if info.url:
        logging.warning("webhook was set (%s) — deleting it to use polling", info.url)
    await bot.delete_webhook(drop_pending_updates=False)
    public_commands = [
        BotCommand(command="start", description="Начало и мой статус"),
        BotCommand(command="days", description="Мои дни"),
        BotCommand(command="tracker", description="Трекер на 30 дней"),
        BotCommand(command="restore", description="Восстановить доступ"),
        BotCommand(command="terms", description="Условия и возврат"),
        BotCommand(command="paysupport", description="Написать в поддержку"),
        BotCommand(command="help", description="Помощь"),
    ]
    admin_commands = public_commands + [
        BotCommand(command="stats", description="Админ: воронка и продажи"),
        BotCommand(command="autopost", description="Админ: автопостинг"),
        BotCommand(command="voice", description="Админ: голосовое к посту"),
        BotCommand(command="audio", description="Админ: аудиопрактика дня"),
        BotCommand(command="next", description="Админ: следующий день сразу"),
        BotCommand(command="reset", description="Админ: сбросить мой прогресс"),
        BotCommand(command="refund", description="Админ: возврат"),
        BotCommand(command="history", description="Админ: история пользователя"),
        BotCommand(command="forget", description="Админ: удалить данные пользователя"),
    ]
    await bot.set_my_commands(public_commands)
    # Админские команды видны в меню только администраторам
    for admin_id in config.admin_ids:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logging.warning("cannot set admin menu for %s (start the bot first?): %s", admin_id, e)
    if not config.admin_ids:
        logging.warning("ADMIN_IDS is empty: admin commands and test access are disabled")
    dp = Dispatcher(config=config, sessionmaker=sessionmaker)
    dp.include_router(payments_router)
    dp.include_router(audio_router)
    dp.include_router(media_router)
    dp.include_router(support_router)
    dp.include_router(router)

    health = await start_health_server()
    scheduler = asyncio.create_task(run_scheduler(bot, sessionmaker, config))
    logging.info(
        "bot started: @%s, payment_mode=%s, autopost=%s, db=%s", me.username, config.payment_mode,
        config.autopost, config.database_url.split("://")[0],
    )
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
