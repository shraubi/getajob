"""Telegram application wiring."""

import logging

from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from jobbot import config
from jobbot.handlers import handle_callback, handle_vacancy_message


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.info("Starting deterministic job bot; resumes: %s", config.RESUME_DIR)

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    vacancy_filter = filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(vacancy_filter, handle_vacancy_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(allowed_updates=["message", "callback_query"])
