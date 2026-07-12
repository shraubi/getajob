import tempfile
import unittest
from pathlib import Path

from token_free import (
    ResumeNotFoundError,
    UnknownDirectionError,
    build_application,
    parse_vacancy,
    render_message,
    select_resume,
)


class TokenFreeFlowTests(unittest.TestCase):
    def test_parses_labelled_vacancy_and_url(self):
        vacancy = parse_vacancy(
            "Title: Senior Python Engineer\nCompany: Acme\n"
            "FastAPI and PostgreSQL\nhttps://jobs.example/42"
        )
        self.assertEqual(vacancy.title, "Senior Python Engineer")
        self.assertEqual(vacancy.company, "Acme")
        self.assertEqual(vacancy.url, "https://jobs.example/42")

    def test_selects_existing_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "backend.pdf"
            resume.write_bytes(b"pdf")
            self.assertEqual(
                select_resume("backend_python", Path(directory), {"backend_python": "backend.pdf"}),
                resume,
            )

    def test_missing_resume_has_actionable_path(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ResumeNotFoundError, "backend.pdf"):
                select_resume(
                    "backend_python", Path(directory), {"backend_python": "backend.pdf"}
                )

    def test_unknown_direction_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(UnknownDirectionError):
                build_application(
                    "Account Executive\nAcme\nOwn enterprise accounts.",
                    Path(directory),
                    {},
                )

    def test_builds_end_to_end_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "backend.pdf"
            resume.write_bytes(b"pdf")
            draft = build_application(
                "Senior Python Engineer\nAcme\nFastAPI, Django, PostgreSQL",
                Path(directory),
                {"backend_python": "backend.pdf"},
            )
            self.assertEqual(draft.direction, "backend_python")
            self.assertEqual(draft.resume_path, resume)
            self.assertIn("Acme", draft.message)

    def test_renders_russian_message_for_russian_vacancy(self):
        vacancy = parse_vacancy("Python-Ñ€Ð°Ð·Ñ€Ð°Ð±Ð¾Ñ‚Ñ‡Ð¸Ðº\nÐšÐ¾Ð¼Ð¿Ð°Ð½Ð¸Ñ: Acme\nFastAPI Ð¸ PostgreSQL")
        self.assertIn("Ð—Ð´Ñ€Ð°Ð²ÑÑ‚Ð²ÑƒÐ¹Ñ‚Ðµ", render_message(vacancy, "backend_python"))


if __name__ == "__main__":
    unittest.main()
