import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).casefold() in {"1", "true", "yes", "on"}


TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
YOUR_CHAT_ID = int(os.environ["YOUR_CHAT_ID"])

# Token-free production path. Real resume files live on the VM and are never committed.
TOKEN_FREE_MODE = _bool_env("TOKEN_FREE_MODE")
RESUME_DIR = Path(os.environ.get("RESUME_DIR", "data/resumes"))
RESUME_FILES = {
    "backend_python": os.environ.get("RESUME_BACKEND_PYTHON", "backend_python.pdf"),
    "data_engineering": os.environ.get("RESUME_DATA_ENGINEERING", "data_engineering.pdf"),
    "ml_engineering": os.environ.get("RESUME_ML_ENGINEERING", "ml_engineering.pdf"),
    "devops": os.environ.get("RESUME_DEVOPS", "devops.pdf"),
}

# Legacy LLM path. Keys are optional until that path is used.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SCORE_MODEL = os.environ.get("SCORE_MODEL", "claude-haiku-4-5-20251001")
GENERATE_MODEL = os.environ.get("GENERATE_MODEL", "claude-sonnet-4-6")
SCORE_THRESHOLD = int(os.environ.get("SCORE_THRESHOLD", "6"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
CHROMA_DIR = os.environ.get("CHROMA_DIR", "storage/chroma_db")
MAX_ANALYZER_ITERATIONS = int(os.environ.get("MAX_ANALYZER_ITERATIONS", "5"))
MAX_WRITER_ITERATIONS = int(os.environ.get("MAX_WRITER_ITERATIONS", "8"))
