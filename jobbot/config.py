import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).casefold() in {"1", "true", "yes", "on"}


TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
YOUR_CHAT_ID = int(os.environ["YOUR_CHAT_ID"])
ADDITIONAL_CHAT_IDS = {
    int(value.strip())
    for value in os.environ.get("ADDITIONAL_CHAT_IDS", "").split(",")
    if value.strip()
}
ALLOWED_CHAT_IDS = {YOUR_CHAT_ID} | ADDITIONAL_CHAT_IDS
RESUME_DIR = Path(os.environ.get("RESUME_DIR", "data/resumes"))
JOBS_DB_PATH = Path(os.environ.get("JOBS_DB_PATH", "storage/jobs.db"))
BOT_READY_FILE = Path(os.environ.get("BOT_READY_FILE", "/tmp/jobbot-ready"))
HIRIFY_EMAIL = os.environ.get("HIRIFY_EMAIL", "")
HIRIFY_PASSWORD = os.environ.get("HIRIFY_PASSWORD", "")
TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.environ.get("TELEGRAM_PHONE", "")
TELEGRAM_SESSION_PATH = Path(os.environ.get("TELEGRAM_SESSION_PATH", "storage/telegram_sender"))
TELEGRAM_SENDING_ENABLED = _bool_env("TELEGRAM_SENDING_ENABLED", True)
TELEGRAM_SEND_MIN_INTERVAL_SECONDS = int(os.environ.get("TELEGRAM_SEND_MIN_INTERVAL_SECONDS", "600"))
TELEGRAM_SEND_MAX_PER_HOUR = int(os.environ.get("TELEGRAM_SEND_MAX_PER_HOUR", "3"))
TELEGRAM_PEER_FLOOD_COOLDOWN_HOURS = int(os.environ.get("TELEGRAM_PEER_FLOOD_COOLDOWN_HOURS", "24"))
TELEGRAM_QUEUE_POLL_SECONDS = float(os.environ.get("TELEGRAM_QUEUE_POLL_SECONDS", "5"))
APPLICATION_PROFILE_PATH = Path(os.environ.get("APPLICATION_PROFILE_PATH", "storage/applicant.json"))
ASHBY_BROWSER_PROFILE_PATH = Path(os.environ.get("ASHBY_BROWSER_PROFILE_PATH", "storage/ashby-browser"))
ATS_BROWSER_HEADLESS = _bool_env(
    "ATS_BROWSER_HEADLESS",
    _bool_env("ASHBY_BROWSER_HEADLESS", True),
)
# Compatibility for existing installations; new provider-neutral code should
# use ATS_BROWSER_HEADLESS.
ASHBY_BROWSER_HEADLESS = ATS_BROWSER_HEADLESS
