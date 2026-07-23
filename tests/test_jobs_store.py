import tempfile
import unittest
from pathlib import Path

from jobbot.integrations.job_page import ParsedJobPage
from jobbot.store import (
    claim_job_for_send,
    claim_telegram_job_for_send,
    complete_telegram_job,
    enqueue_telegram_job,
    get_due_telegram_job,
    get_job_by_prefix,
    get_telegram_queue_item,
    mark_job_sent,
    save_fetched_job,
    set_sender_cooldown,
)
from datetime import datetime, timedelta, timezone
from jobbot.application import Vacancy


class JobsStoreTests(unittest.TestCase):
    def test_upserts_fetched_job_with_contact(self):
        vacancy = Vacancy("Backend Engineer", "Acme", "Python backend role with APIs", "https://hirify.me/jobs/1-role")
        page = ParsedJobPage(vacancy, "telegram_contact", "https://t.me/recruiter", "https://hirify.me/jobs/1-role", "telegram", "@recruiter")
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            first = save_fetched_job(db, page, "backend_python", "backend.pdf", "Hello")
            second = save_fetched_job(db, page, "backend_python", "backend-v2.pdf", "Hello again")
            self.assertEqual(first, second)
            stored = get_job_by_prefix(db, first[:24])
            self.assertEqual(stored["recruiter_message"], "Hello again")
            self.assertTrue(claim_job_for_send(db, first))
            self.assertFalse(claim_job_for_send(db, first))
            self.assertTrue(mark_job_sent(db, first, 42))
            self.assertFalse(claim_job_for_send(db, first))

    def test_persistent_telegram_cooldown_blocks_new_send_claims(self):
        vacancy = Vacancy("Backend Engineer", "Acme", "Python backend role", "https://hirify.me/jobs/2-role")
        page = ParsedJobPage(vacancy, "telegram_contact", "https://t.me/recruiter", vacancy.url, "telegram", "recruiter")
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            job_id = save_fetched_job(db, page, "backend_python", "backend.pdf", "Hello")
            blocked_until = datetime.now(timezone.utc) + timedelta(hours=24)
            set_sender_cooldown(db, "telegram", blocked_until, "Telegram PeerFlood")
            claimed, retry_at, reason = claim_telegram_job_for_send(
                db, job_id, min_interval_seconds=600, max_per_hour=3
            )
            self.assertFalse(claimed)
            self.assertEqual(retry_at, blocked_until)
            self.assertEqual(reason, "Telegram PeerFlood")

    def test_telegram_queue_is_durable_and_idempotent(self):
        vacancy = Vacancy(
            "Backend Engineer", "Acme", "Python backend role",
            "https://hirify.me/jobs/3-role",
        )
        page = ParsedJobPage(
            vacancy, "telegram_contact", "https://t.me/recruiter",
            vacancy.url, "telegram", "recruiter",
        )
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            job_id = save_fetched_job(
                db, page, "backend_python", "backend.pdf", "Hello",
            )
            now = datetime.now(timezone.utc)
            self.assertTrue(enqueue_telegram_job(
                db, job_id, available_at=now, reason="user_confirmed",
                interaction_id="callback:1", notify_chat_id=7,
            ))
            self.assertFalse(enqueue_telegram_job(
                db, job_id, available_at=now, reason="duplicate",
                interaction_id="callback:2", notify_chat_id=7,
            ))
            item = get_telegram_queue_item(db, job_id)
            self.assertEqual(item["interaction_id"], "callback:1")
            self.assertEqual(item["notify_chat_id"], 7)
            due = get_due_telegram_job(db, now=now + timedelta(seconds=1))
            self.assertEqual(due["id"], job_id)
            self.assertEqual(due["status"], "queued")
            complete_telegram_job(db, job_id)
            self.assertIsNone(get_telegram_queue_item(db, job_id))


if __name__ == "__main__":
    unittest.main()
