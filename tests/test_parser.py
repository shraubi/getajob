import tempfile
import unittest
from pathlib import Path

from bot.parser import JobSource, classify_source, parse_html
from storage.database import JobStore


class ParserTests(unittest.TestCase):
    def test_parses_and_persists_schema_org_jobposting(self):
        html = Path("tests/fixtures/jobposting.html").read_text(encoding="utf-8")
        vacancy = parse_html(html, "https://jobs.example/vacancy/42")
        self.assertEqual(vacancy.title, "Python Developer")
        self.assertEqual(vacancy.company, "Acme SAS")
        self.assertEqual(vacancy.description, "Build reliable APIs and data pipelines.")
        self.assertEqual(vacancy.application_url, "https://jobs.example/apply/42")
        self.assertIs(vacancy.source, JobSource.GENERIC_WEB)

        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            job_id = store.save_job(vacancy)
            self.assertEqual(job_id, store.save_job(vacancy))
            self.assertTrue(store.begin_action("telegram:42", job_id))
            self.assertFalse(store.begin_action("telegram:42", job_id))
            store.close()

    def test_source_classification(self):
        cases = {
            "https://www.hellowork.com/fr-fr/emplois/1.html": JobSource.HELLOWORK,
            "https://t.me/jobs/123": JobSource.TELEGRAM,
            "https://example.com/jobs/1": JobSource.GENERIC_WEB,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertIs(classify_source(url), expected)


if __name__ == "__main__":
    unittest.main()
