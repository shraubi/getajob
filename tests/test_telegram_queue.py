import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("YOUR_CHAT_ID", "1")

from jobbot import config
from jobbot.application import Vacancy
from jobbot.integrations.job_page import ParsedJobPage
from jobbot.integrations.telegram_sender import TelegramPeerFloodError
from jobbot.store import (
    enqueue_telegram_job,
    get_telegram_queue_item,
    save_fetched_job,
    set_sender_cooldown,
)
from jobbot.telegram_queue import process_telegram_queue_once


class PeerFloodSender:
    def __init__(self, *_args):
        pass

    async def send_resume(self, *_args):
        raise TelegramPeerFloodError("restricted")


class TelegramQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_peer_flood_keeps_application_queued(self):
        vacancy = Vacancy(
            "Python Engineer", "Acme", "Python backend role",
            "https://hirify.me/jobs/queued-role",
        )
        page = ParsedJobPage(
            vacancy, "telegram_contact", "https://t.me/recruiter",
            vacancy.url, "telegram", "recruiter",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "jobs.db"
            (root / "backend.pdf").write_bytes(b"pdf")
            job_id = save_fetched_job(
                db, page, "backend_python", "backend.pdf", "Hello",
            )
            enqueue_telegram_job(
                db,
                job_id,
                available_at=datetime.now(timezone.utc),
                reason="user_confirmed",
                interaction_id="callback:queue",
            )
            with (
                patch("jobbot.telegram_queue.TelegramSender", PeerFloodSender),
                patch.object(config, "JOBS_DB_PATH", db),
                patch.object(config, "RESUME_DIR", root),
                patch.object(config, "TELEGRAM_API_ID", 1),
                patch.object(config, "TELEGRAM_API_HASH", "hash"),
                patch.object(config, "TELEGRAM_SEND_MIN_INTERVAL_SECONDS", 0),
                patch.object(config, "TELEGRAM_SEND_MAX_PER_HOUR", 0),
                patch.object(config, "TELEGRAM_PEER_FLOOD_COOLDOWN_HOURS", 24),
            ):
                self.assertTrue(await process_telegram_queue_once())

            queued = get_telegram_queue_item(db, job_id)
            self.assertIsNotNone(queued)
            self.assertEqual(queued["status"], "paused")
            self.assertEqual(queued["reason"], "peer_flood")
            self.assertEqual(queued["attempts"], 1)
            connection = sqlite3.connect(db)
            try:
                event = connection.execute(
                    """SELECT data_json FROM jobbot_review_events
                       WHERE event_type='telegram_throttled'
                       ORDER BY id DESC LIMIT 1"""
                ).fetchone()
            finally:
                connection.close()
            self.assertTrue(json.loads(event[0])["queue_present"])

    async def test_cooldown_defers_without_contacting_telegram(self):
        vacancy = Vacancy(
            "Python Engineer", "Acme", "Python backend role",
            "https://hirify.me/jobs/cooldown-role",
        )
        page = ParsedJobPage(
            vacancy, "telegram_contact", "https://t.me/recruiter",
            vacancy.url, "telegram", "recruiter",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "jobs.db"
            job_id = save_fetched_job(
                db, page, "backend_python", "backend.pdf", "Hello",
            )
            enqueue_telegram_job(
                db,
                job_id,
                available_at=datetime.now(timezone.utc),
                reason="user_confirmed",
                interaction_id="callback:cooldown",
            )
            blocked_until = datetime.now(timezone.utc) + timedelta(hours=24)
            set_sender_cooldown(
                db, "telegram", blocked_until, "Telegram PeerFlood",
            )
            with (
                patch(
                    "jobbot.telegram_queue.TelegramSender",
                    side_effect=AssertionError("must not contact Telegram"),
                ),
                patch.object(config, "JOBS_DB_PATH", db),
                patch.object(config, "TELEGRAM_SEND_MIN_INTERVAL_SECONDS", 0),
                patch.object(config, "TELEGRAM_SEND_MAX_PER_HOUR", 0),
            ):
                self.assertFalse(await process_telegram_queue_once())

            queued = get_telegram_queue_item(db, job_id)
            self.assertEqual(queued["status"], "pending")
            self.assertEqual(queued["reason"], "Telegram PeerFlood")
            self.assertEqual(queued["attempts"], 0)
            self.assertGreaterEqual(
                datetime.fromisoformat(queued["available_at"]),
                blocked_until,
            )


if __name__ == "__main__":
    unittest.main()
