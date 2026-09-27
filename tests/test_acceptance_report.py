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
                       created_at TEXT, updated_at TEXT
                   );"""
            )

    def tearDown(self):
        self.temp.cleanup()

    def _offer(self, offer_id, status, updated_at):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """INSERT INTO inbound_offers
                   VALUES ('hellowork', ?, ?, ?, ?)""",
                (offer_id, status, self.since.isoformat(), updated_at.isoformat()),
            )

    def test_waits_when_no_live_offer_has_arrived(self):
        report = assess(self.db, since=self.since, now=self.now)
        self.assertEqual(report["status"], "WAITING_FOR_LIVE_TRAFFIC")

    def test_reports_recorded_completion_without_claiming_external_verification(self):
        self._offer("123", "completed", self.now)
        report = assess(self.db, since=self.since, now=self.now)
        self.assertEqual(report["status"], "RECORDED_COMPLETION")
        self.assertEqual(report["offer_statuses"], {"completed": 1})

    def test_fails_on_stale_queue_and_redacts_error_text(self):
        self._offer("123", "processing", self.now - timedelta(minutes=40))
        self._offer("456", "failed", self.now)
        report = assess(self.db, since=self.since, now=self.now)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("offers_stuck_in_queue", report["reasons"])
        self.assertIn("applications_failed", report["reasons"])
        self.assertEqual(report["stale_offer_ids"], ["123"])
        self.assertNotIn("canonical_url", str(report))


if __name__ == "__main__":
    unittest.main()
