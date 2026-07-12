import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from token_free import (
    ResumeNotFoundError,
    UnknownDirectionError,
    build_application,
    classify_resume,
    discover_resumes,
    parse_vacancy,
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

    @patch("token_free.extract_resume_text", return_value="Python FastAPI Django PostgreSQL")
    def test_classifies_resume_from_pdf_text(self, _extract):
        self.assertEqual(classify_resume(Path("generic.pdf")), "backend_python")

    @patch("token_free.extract_resume_text", side_effect=ValueError("scanned"))
    def test_classifies_scanned_resume_from_filename(self, _extract):
        self.assertEqual(classify_resume(Path("python_backend.pdf")), "backend_python")

    @patch("token_free.extract_resume_text", return_value="")
    def test_splits_camel_case_filename_and_logs_scores(self, _extract):
        with self.assertLogs("token_free", level="INFO") as logs:
            direction = classify_resume(Path("Ekaterina_Tuganova_DataEngineer_v2.pdf"))
        self.assertEqual(direction, "data_engineering")
        self.assertIn("extracted_chars=0", logs.output[0])
        self.assertIn("scores=", logs.output[0])

    @patch("token_free.extract_resume_text", return_value="Python FastAPI Django PostgreSQL")
    def test_discovers_and_selects_resume(self, _extract):
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"pdf")
            self.assertEqual(discover_resumes(Path(directory))["backend_python"], resume)
            self.assertEqual(select_resume("backend_python", Path(directory)), resume)

    def test_missing_resume_has_actionable_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ResumeNotFoundError, "backend_python"):
                select_resume("backend_python", Path(directory))

    def test_unknown_direction_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(UnknownDirectionError):
                build_application(
                    "Account Executive\nAcme\nOwn enterprise accounts.", Path(directory)
                )

    @patch("token_free.extract_resume_text", return_value="Python FastAPI Django PostgreSQL")
    def test_builds_end_to_end_draft(self, _extract):
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"pdf")
            draft = build_application(
                "Senior Python Engineer\nAcme\nFastAPI, Django, PostgreSQL",
                Path(directory),
            )
            self.assertEqual(draft.direction, "backend_python")
            self.assertEqual(draft.resume_path, resume)
            self.assertIn("Acme", draft.message)


if __name__ == "__main__":
    unittest.main()
