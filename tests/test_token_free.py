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
from classifier import classify


class TokenFreeFlowTests(unittest.TestCase):
    def test_matches_russian_ai_agent_role_to_ml_resume(self):
        title = "Разработчик ИИ-агентов (Маркетинг)"
        description = "Навыки: ai, marketing, api, llm, automation, databases"
        self.assertEqual(classify(title, description), "ml_engineering")

    def test_matches_python_backend_without_requiring_exact_title(self):
        title = "Middle Developer"
        description = "Backend service using Python, FastAPI, PostgreSQL, Redis and Kafka"
        self.assertEqual(classify(title, description), "backend_python")

    def test_does_not_treat_typescript_backend_tools_as_devops(self):
        title = "Backend Engineer (TypeScript)"
        description = (
            "NestJS, Docker, CI/CD, PostgreSQL. Work with platform engineering, "
            "SRE practices, Kubernetes infrastructure and Terraform deployments."
        )
        self.assertEqual(classify(title, description), "other")

    def test_does_not_match_android_role_from_incidental_backend_or_devops_words(self):
        title = "Android Developer (Kotlin)"
        description = "Backend APIs, Docker CI/CD and infrastructure collaboration"
        self.assertEqual(classify(title, description), "other")

    def test_matches_russian_technical_support_specialist(self):
        self.assertEqual(
            classify("Специалист технической поддержки", "Помощь пользователям и обработка обращений"),
            "tech_support",
        )

    def test_matches_payment_support_manager(self):
        self.assertEqual(
            classify("Payment Support Manager (iGaming)", "Handle payment incidents and customer tickets"),
            "tech_support",
        )

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

    @patch(
        "token_free.extract_resume_text",
        return_value="Ekaterina Tuganova\nTechnical Support Engineer\nLinux troubleshooting and ticketing",
    )
    def test_uses_second_pdf_line_as_resume_role(self, _extract):
        with self.assertLogs("token_free", level="INFO") as logs:
            direction = classify_resume(Path("Ekaterina_Tuganova_Resume (1).pdf"))
        self.assertEqual(direction, "tech_support")
        self.assertIn("role_hint='Technical Support Engineer'", logs.output[0])
        self.assertIn("source=pdf_text", logs.output[0])

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
            self.assertEqual(draft.vacancy.company, "Unknown company")
            self.assertNotIn("Unknown company", draft.message)


if __name__ == "__main__":
    unittest.main()
