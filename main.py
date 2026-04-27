import logging
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters
from bot.handlers import handle_vacancy_message, handle_callback

logging.basicConfig(level=logging.INFO)

def run():
    from config import TELEGRAM_BOT_TOKEN
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    vacancy_filter = filters.TEXT & ~filters.COMMAND
    # Listen to regular messages AND channel posts (forwarded or native)
    app.add_handler(MessageHandler(vacancy_filter, handle_vacancy_message))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS & vacancy_filter, handle_vacancy_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.run_polling(allowed_updates=["message", "channel_post", "callback_query"])

if __name__ == "__main__":
    run()
