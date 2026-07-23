"""Telegram application wiring."""

import asyncio
import logging
from contextlib import suppress

from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from jobbot import config
from jobbot.handlers import handle_callback, handle_vacancy_message
from jobbot.logging_config import configure_logging
from jobbot.telegram_queue import telegram_queue_worker

logger = logging.getLogger(__name__)


def _clear_ready_file() -> None:
    with suppress(FileNotFoundError):
        config.BOT_READY_FILE.unlink()


async def _mark_ready(application: Application) -> None:
    """Publish readiness only after Telegram accepted the bot token."""
    if config.TELEGRAM_SENDING_ENABLED:
        application.bot_data["telegram_queue_task"] = application.create_task(
            telegram_queue_worker(application.bot),
            name="telegram-send-queue",
        )
    config.BOT_READY_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.BOT_READY_FILE.write_text("ready\n", encoding="utf-8")
    logger.info("Telegram polling initialized as @%s", application.bot.username)


async def _mark_stopped(application: Application) -> None:
    task = application.bot_data.pop("telegram_queue_task", None)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    _clear_ready_file()


def run() -> None:
    configure_logging()
    _clear_ready_file()
    logger.info("Starting deterministic job bot; resumes: %s", config.RESUME_DIR)

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_mark_ready)
        .post_shutdown(_mark_stopped)
        .build()
    )
    vacancy_filter = filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(vacancy_filter, handle_vacancy_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(allowed_updates=["message", "callback_query"])
