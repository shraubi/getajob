"""Telegram application wiring."""

import asyncio
import hashlib
import hmac
import logging
import re
from contextlib import suppress

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from jobbot import config
from jobbot.handlers import handle_callback, handle_vacancy_message
from jobbot.logging_config import configure_logging
from jobbot.telegram_queue import telegram_queue_worker
from jobbot.email_ingest import hellowork_email_worker

logger = logging.getLogger(__name__)


def _clear_ready_file() -> None:
    with suppress(FileNotFoundError):
        config.BOT_READY_FILE.unlink()


async def _health_command(update, context) -> None:
    """Reply to an allowlisted user or a bot-token-signed production probe."""
    chat = update.effective_chat
    message = update.effective_message
    nonce = context.args[0] if context.args else ""
    if not chat or not message or not re.fullmatch(r"[a-f0-9]{32}", nonce):
        return
    signature = context.args[1] if len(context.args) > 1 else ""
    expected = hmac.new(
        config.TELEGRAM_BOT_TOKEN.encode(), nonce.encode(), hashlib.sha256
    ).hexdigest()
    if chat.id not in config.ALLOWED_CHAT_IDS and not hmac.compare_digest(
        signature, expected
    ):
        return
    await message.reply_text(f"jobbot-ready:{nonce}")


async def _mark_ready(application: Application) -> None:
    """Publish readiness only after Telegram accepted the bot token."""
    if config.TELEGRAM_SENDING_ENABLED:
        application.bot_data["telegram_queue_task"] = application.create_task(
            telegram_queue_worker(application.bot),
            name="telegram-send-queue",
        )
    if config.HELLOWORK_EMAIL_INGEST_ENABLED:
        application.bot_data["hellowork_email_task"] = application.create_task(
            hellowork_email_worker(application.bot),
            name="hellowork-email-intake",
        )
    config.BOT_READY_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.BOT_READY_FILE.write_text("ready\n", encoding="utf-8")
    logger.info("Telegram polling initialized as @%s", application.bot.username)


async def _mark_stopped(application: Application) -> None:
    for key in ("telegram_queue_task", "hellowork_email_task"):
        task = application.bot_data.pop(key, None)
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
    app.add_handler(CommandHandler("health", _health_command))
    app.add_handler(MessageHandler(vacancy_filter, handle_vacancy_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(allowed_updates=["message", "callback_query"])
