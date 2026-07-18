"""Telegram application wiring."""

import logging
from contextlib import suppress

from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from jobbot import config
from jobbot.handlers import handle_callback, handle_vacancy_message
from jobbot.logging_config import configure_logging

logger = logging.getLogger(__name__)


def _clear_ready_file() -> None:
    with suppress(FileNotFoundError):
        config.BOT_READY_FILE.unlink()


async def _mark_ready(application: Application) -> None:
    """Publish readiness only after Telegram accepted the bot token."""
    config.BOT_READY_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.BOT_READY_FILE.write_text("ready\n", encoding="utf-8")
    logger.info("Telegram polling initialized as @%s", application.bot.username)


async def _mark_stopped(_application: Application) -> None:
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
