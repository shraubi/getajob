import logging

from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

import config
from bot.handlers import handle_callback, handle_vacancy_message

logging.basicConfig(level=logging.INFO)


def run():
    if config.TOKEN_FREE_MODE:
        logging.info("Starting in token-free mode; resumes: %s", config.RESUME_DIR)
    else:
        from rag.indexer import index_candidate_profile

        logging.info("Indexing candidate profile...")
        index_candidate_profile()
        logging.info("Profile indexed. Starting legacy LLM bot.")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    vacancy_filter = filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(vacancy_filter, handle_vacancy_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    run()
