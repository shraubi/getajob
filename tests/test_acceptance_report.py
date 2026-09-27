import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.acceptance_report import assess


class AcceptanceReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "jobs.db"
        self.now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
        self.since = self.now - timedelta(hours=1)
        with sqlite3.connect(self.db) as connection:
            connection.executescript(
                """CREATE TABLE inbound_email_messages (
                       status TEXT, first_seen_at TEXT, handled_at TEXT
                   );
                   CREATE TABLE inbound_offers (
                       provider TEXT, offer_id TEXT, status TEXT,
                       last_error TEXT, created_at TEXT, updated_at TEXT
                   );"""
            )

    def tearDown(self):
        self.temp.cleanup()

    def _offer(self, offer_id, status, updated_at, detail=""):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """INSERT INTO inbound_offers
                   VALUES ('hellowork', ?, ?, ?, ?, ?)""",
                (offer_id, status, detail, self.since.isoformat(), updated_at.isoformat()),
            )

    def test_waits_when_no_live_offer_has_arrived(self):
        report = assess(self.db, since=self.since, now=self.now)
        self.assertEqual(report["status"], "WAITING_FOR_LIVE_TRAFFIC")

    def test_green_requires_fresh_account_marker_for_every_completed_offer(self):
        self._offer("123", "completed", self.now, "account_marker_recheck=1 completed_steps=2")
        self._offer("124", "completed", self.now, "account_marker_recheck=1")
        report = assess(self.db, since=self.since, now=self.now)
        self.assertEqual(report["status"], "RECHECKED_APPLIED")
        self.assertEqual(report["offer_statuses"], {"completed": 2})
        self.assertEqual(report["rechecked_offer_count"], 2)
        self.assertEqual(report["unverified_completed_count"], 0)

    def test_legacy_or_unverified_completion_is_not_green(self):
        self._offer("123", "completed", self.now, "confirmed")
        self._offer("124", "completed", self.now, "account_marker_recheck=0")
        report = assess(self.db, since=self.since, now=self.now)
        self.assertEqual(report["status"], "UNVERIFIED_COMPLETION")
        self.assertEqual(report["unverified_completed_count"], 2)
        self.assertIn("account_marker_not_rechecked", report["reasons"])

    def test_pending_offer_keeps_assessment_waiting(self):
        self._offer("123", "completed", self.now, "account_marker_recheck=1")
        self._offer("124", "pending", self.now)
        report = assess(self.db, since=self.since, now=self.now)
        self.assertEqual(report["status"], "WAITING_FOR_TERMINAL_OUTCOME")

    def test_fails_on_stale_queue_and_redacts_error_text(self):
        self._offer("123", "processing", self.now - timedelta(minutes=40))
        self._offer("456", "failed", self.now, "private@example.com")
        report = assess(self.db, since=self.since, now=self.now)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("offers_stuck_in_queue", report["reasons"])
        self.assertIn("applications_failed", report["reasons"])
        self.assertEqual(report["stale_offer_count"], 1)
        self.assertEqual(report["attention_offer_count"], 1)
        self.assertNotIn("123", str(report))
        self.assertNotIn("456", str(report))
        self.assertNotIn("private@example.com", str(report))


if __name__ == "__main__":
    unittest.main()
