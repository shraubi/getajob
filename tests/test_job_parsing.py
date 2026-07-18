import tempfile
import unittest
from pathlib import Path

from jobbot.store import get_job, save_job
from jobbot.application import format_vacancy_summary, parse_vacancy, render_message, render_telegram_message

LEAD = """Databricks Data Engineer in NDA

47 000 - 72 000 EUR

Remote | Full-time | middle | B2 | Netherlands | Backend

English B2 required

Skills: python, sql, etl, data modeling, data warehousing, databricks, pyspark

By subscription: dsa"""


class JobParsingTests(unittest.TestCase):
    def test_parses_telegram_lead_without_using_salary_as_company(self):
        vacancy = parse_vacancy(LEAD)
        self.assertEqual(vacancy.title, "Databricks Data Engineer in NDA")
        self.assertEqual(vacancy.company, "Unknown company")
        self.assertEqual(vacancy.salary, "47 000 - 72 000 EUR")
        self.assertEqual(vacancy.source_category, "telegram_lead")
        self.assertEqual(vacancy.work_format, "Remote")
        self.assertEqual(vacancy.employment, "Full-time")
        self.assertEqual(vacancy.seniority, "middle")
        self.assertEqual(vacancy.location, "Netherlands")
        self.assertIn("databricks", vacancy.skills)

    def test_does_not_generate_generic_cover_message(self):
        vacancy = parse_vacancy(LEAD)
        message = render_message(vacancy, "data_engineering")
        self.assertEqual(message, "")
        summary = format_vacancy_summary(vacancy, "data_engineering", "abc123")
        self.assertIn("Source: telegram_lead", summary)
        self.assertIn("Salary: 47 000 - 72 000 EUR", summary)

    def test_renders_fixed_russian_telegram_message_with_vacancy_link(self):
        url = "https://hirify.me/jobs/732017-application-backend-engineer-python"
        self.assertEqual(
            render_telegram_message(url),
            "Приветствую, хочу откликнуться вот на эту вакансию:\n"
            f"{url}\n"
            "Резюме прикрепляю. Буду рада пообщаться подробнее",
        )

    def test_persists_and_updates_parsed_job(self):
        vacancy = parse_vacancy(LEAD)
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "jobs.db"
            first = save_job(db_path, vacancy, "data_engineering", "data.pdf")
            second = save_job(db_path, vacancy, "data_engineering", "data-v2.pdf")
            self.assertEqual(first, second)
            stored = get_job(db_path, first)
            self.assertEqual(stored["source_category"], "telegram_lead")
            self.assertEqual(stored["resume_name"], "data-v2.pdf")


if __name__ == "__main__":
    unittest.main()
