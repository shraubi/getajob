import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY       = os.environ["ANTHROPIC_API_KEY"]
YOUR_CHAT_ID            = int(os.environ["YOUR_CHAT_ID"])
VACANCY_SOURCE_CHAT_ID  = int(os.environ["VACANCY_SOURCE_CHAT_ID"])  # the channel with vacancies

SCORE_MODEL    = "claude-haiku-4-5-20251001"
GENERATE_MODEL = "claude-sonnet-4-6"

SCORE_THRESHOLD = 6
