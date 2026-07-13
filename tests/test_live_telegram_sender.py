"""Explicitly enabled live smoke test for the Telegram user-account sender."""

import asyncio
import os
import unittest
from pathlib import Path

from telegram_sender import TelegramSender


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_TELEGRAM_SMOKE") == "1",
    "set RUN_LIVE_TELEGRAM_SMOKE=1 to send to a controlled test account",
)
class LiveTelegramSenderTests(unittest.TestCase):
    def test_sends_resume_to_controlled_account(self):
        required = {
            "TELEGRAM_API_ID": os.environ.get("TELEGRAM_API_ID", ""),
            "TELEGRAM_API_HASH": os.environ.get("TELEGRAM_API_HASH", ""),
            "TELEGRAM_SESSION_PATH": os.environ.get("TELEGRAM_SESSION_PATH", ""),
            "LIVE_TELEGRAM_TARGET": os.environ.get("LIVE_TELEGRAM_TARGET", ""),
            "LIVE_TELEGRAM_RESUME": os.environ.get("LIVE_TELEGRAM_RESUME", ""),
        }
        missing = [name for name, value in required.items() if not value]
        self.assertFalse(missing, f"missing live smoke settings: {', '.join(missing)}")

        sender = TelegramSender(
            int(required["TELEGRAM_API_ID"]),
            required["TELEGRAM_API_HASH"],
            Path(required["TELEGRAM_SESSION_PATH"]),
        )
        message_id = asyncio.run(
            sender.send_resume(
                required["LIVE_TELEGRAM_TARGET"],
                "getajob live smoke test â€” no reply needed",
                Path(required["LIVE_TELEGRAM_RESUME"]),
            )
        )
        self.assertGreater(message_id, 0)


if __name__ == "__main__":
    unittest.main()

