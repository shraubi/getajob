import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from jobbot.review_events import record_review_event
from ralph.event_review import analyze_events, read_event_batch

class RalphEventReviewTests(unittest.TestCase):
    def test_journal_paginates_chronologically(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            for index in range(31):
                record_review_event(db, f"1:{index}", "job_previewed",
                    source_url=f"https://jobs.example/{index}", title="Python Engineer",
                    direction="backend_python", resume_preview=True, application_path=True)
            events, has_more = read_event_batch(db, after_id=0)
            self.assertEqual(len(events), 30)
            self.assertTrue(has_more)
            tail, tail_has_more = read_event_batch(db, after_id=events[-1].id)
            self.assertEqual(len(tail), 1)
            self.assertFalse(tail_has_more)

    def test_detects_structured_live_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            record_review_event(db, "7:10", "role_rejected",
                source_url="https://jobs.example/support",
                title="Technical Support Specialist", scores={"tech_support": 14})
            record_review_event(db, "7:11", "telegram_throttled",
                source_url="https://jobs.example/send",
                reason="minimum interval", queue_present=False)
            events, _ = read_event_batch(db, after_id=0)
            rules = {finding.rule_id for finding in analyze_events(events)}
            self.assertTrue({"support_role_misclassified", "telegram_throttled",
                             "telegram_queue_missing"} <= rules)

    def test_ignores_expired_pages_and_stores_no_transcript_field(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            record_review_event(db, "1:1", "job_fetch_failed",
                source_url="https://jobs.example/expired",
                blocker_type="HTTP404", expired=True)
            events, _ = read_event_batch(db, after_id=0)
            self.assertEqual(analyze_events(events), ())
            data = json.loads(sqlite3.connect(db).execute(
                "SELECT data_json FROM jobbot_review_events").fetchone()[0])
            self.assertNotIn("text", data)

if __name__ == "__main__":
    unittest.main()
