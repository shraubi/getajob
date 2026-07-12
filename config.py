import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).casefold() in {"1", "true", "yes", "on"}


TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
YOUR_CHAT_ID = int(os.environ["YOUR_CHAT_ID"])
TOKEN_FREE_MODE = _bool_env("TOKEN_FREE_MODE")
RESUME_DIR = Path(os.environ.get("RESUME_DIR", "data/resumes"))
JOBS_DB_PATH = Path(os.environ.get("JOBS_DB_PATH", "storage/jobs.db"))
HIRIFY_EMAIL = os.environ.get("HIRIFY_EMAIL", "")
HIRIFY_PASSWORD = os.environ.get("HIRIFY_PASSWORD", "")
TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.environ.get("TELEGRAM_PHONE", "")
TELEGRAM_SESSION_PATH = Path(os.environ.get("TELEGRAM_SESSION_PATH", "storage/telegram_sender"))

# Optional legacy LLM path.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SCORE_MODEL = os.environ.get("SCORE_MODEL", "claude-haiku-4-5-20251001")
GENERATE_MODEL = os.environ.get("GENERATE_MODEL", "claude-sonnet-4-6")
SCORE_THRESHOLD = int(os.environ.get("SCORE_THRESHOLD", "6"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
CHROMA_DIR = os.environ.get("CHROMA_DIR", "storage/chroma_db")
MAX_ANALYZER_ITERATIONS = int(os.environ.get("MAX_ANALYZER_ITERATIONS", "5"))
MAX_WRITER_ITERATIONS = int(os.environ.get("MAX_WRITER_ITERATIONS", "8"))
