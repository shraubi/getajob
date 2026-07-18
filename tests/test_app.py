import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("YOUR_CHAT_ID", "1")

from jobbot import config
from jobbot.app import _mark_ready, _mark_stopped
from jobbot.logging_config import configure_logging


class AppLoggingTests(unittest.TestCase):
    def test_dependency_request_logs_cannot_expose_telegram_token_urls(self):
        httpx_logger = logging.getLogger("httpx")
        httpcore_logger = logging.getLogger("httpcore")
        previous_httpx = httpx_logger.level
        previous_httpcore = httpcore_logger.level
        try:
            httpx_logger.setLevel(logging.INFO)
            httpcore_logger.setLevel(logging.INFO)
            configure_logging()
            self.assertGreaterEqual(httpx_logger.level, logging.WARNING)
            self.assertGreaterEqual(httpcore_logger.level, logging.WARNING)
        finally:
            httpx_logger.setLevel(previous_httpx)
            httpcore_logger.setLevel(previous_httpcore)


class AppReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_tracks_authenticated_telegram_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            ready_file = Path(directory) / "jobbot-ready"
            application = SimpleNamespace(bot=SimpleNamespace(username="job_bot"))
            with patch.object(config, "BOT_READY_FILE", ready_file):
                await _mark_ready(application)
                self.assertEqual(ready_file.read_text(encoding="utf-8"), "ready\n")
                await _mark_stopped(application)
                self.assertFalse(ready_file.exists())


if __name__ == "__main__":
    unittest.main()
