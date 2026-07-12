import tempfile
import unittest
from pathlib import Path

from job_page import ParsedJobPage
from jobs_store import save_fetched_job
from token_free import Vacancy


class JobsStoreTests(unittest.TestCase):
    def test_upserts_fetched_job_with_contact(self):
        vacancy = Vacancy("Backend Engineer", "Acme", "Python backend role with APIs", "https://hirify.me/jobs/1-role")
        page = ParsedJobPage(vacancy, "telegram_contact", "https://t.me/recruiter", "https://hirify.me/jobs/1-role", "telegram", "@recruiter")
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            first = save_fetched_job(db, page, "backend_python", "backend.pdf")
            second = save_fetched_job(db, page, "backend_python", "backend-v2.pdf")
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
