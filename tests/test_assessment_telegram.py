import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scripts import assessment_telegram
from scripts.assessment_telegram import format_message, should_send, _signature


class AssessmentTelegramTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 7, 14, 33, tzinfo=timezone.utc)
        self.report = {
            "status": "FAIL",
            "reasons": ["application_gate_closed"],
            "application_gate_closed": True,
            "email_statuses": {"queued": 4},
            "offer_statuses": {"held": 52},
            "rechecked_new_submission_count": 0,
            "active_offer_count": 0,
            "held_offer_count": 1949,
        }

    def test_report_explains_silent_application_stop(self):
        message = format_message(self.report, "healthy", "current", self.now)
        self.assertIn("лимит одной попытки уже использован", message)
        self.assertIn("Подтверждённых новых откликов: 0", message)
        self.assertIn("Всего удержано вакансий: 1949", message)
        self.assertIn("писем принято 4", message)

    def test_new_held_offers_alert_after_six_hours_without_hourly_spam(self):
        state = {
            "last_sent_at": (self.now - timedelta(hours=5)).isoformat(),
            "signature": _signature(self.report, "healthy", "current"),
            "held_offer_count": 1900,
        }
        self.assertFalse(should_send(self.report, "healthy", "current", state, self.now))
        state["last_sent_at"] = (self.now - timedelta(hours=6)).isoformat()
        self.assertTrue(should_send(self.report, "healthy", "current", state, self.now))

    def test_status_change_or_daily_digest_sends(self):
        state = {
            "last_sent_at": self.now.isoformat(),
            "signature": _signature(self.report, "healthy", "current"),
            "held_offer_count": 1949,
        }
        self.assertTrue(should_send(self.report, "unhealthy", "current", state, self.now))
        self.assertTrue(should_send(self.report, "healthy", "stale", state, self.now))
        self.assertTrue(should_send(
            self.report, "healthy", "current", state,
            self.now + timedelta(hours=24),
        ))

    def test_cli_sends_once_and_persists_cooldown_only_after_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            state_path = Path(directory) / "assessment-telegram.json"
            with closing(sqlite3.connect(db)) as connection:
                connection.executescript(
                    """CREATE TABLE inbound_email_messages
                       (status TEXT, first_seen_at TEXT, handled_at TEXT);
                       CREATE TABLE inbound_offers
                       (provider TEXT, status TEXT, last_error TEXT,
                        created_at TEXT, updated_at TEXT);
                       CREATE TABLE inbound_offer_controls (key TEXT PRIMARY KEY);
                       INSERT INTO inbound_offer_controls VALUES ('single_live_attempt_v1');"""
                )
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    "INSERT INTO inbound_offers VALUES ('hellowork', 'held', '', ?, ?)",
                    (now, now),
                )
                connection.commit()
            sender = AsyncMock()
            args = ["assessment_telegram", "--db", str(db), "--state", str(state_path),
                    "--bot-health", "healthy", "--release-status", "current"]
            with (
                patch.object(assessment_telegram, "_send", sender),
                patch("sys.argv", args),
                patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test", "YOUR_CHAT_ID": "1"}),
            ):
                self.assertEqual(assessment_telegram.main(), 0)
                self.assertEqual(assessment_telegram.main(), 0)
            sender.assert_awaited_once()
            self.assertTrue(json.loads(state_path.read_text())["signature"])


if __name__ == "__main__":
    unittest.main()

