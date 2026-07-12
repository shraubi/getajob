import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("YOUR_CHAT_ID", "1")

from bot.handlers import _handle_token_free
from job_page import JobPageError, ParsedJobPage
from token_free import ApplicationDraft, Vacancy


class FakeBot:
    def __init__(self):
        self.messages = []
        self.documents = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)

    async def send_document(self, **kwargs):
        self.documents.append(kwargs)


class FakeHirifyClient:
    def __init__(self, *_args, **_kwargs):
        pass

    async def get_contact(self, _url):
        return SimpleNamespace(kind="telegram", value="brandiumsu", target_url="https://t.me/brandiumsu")


class TokenFreeHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_job_posting_does_not_require_source_category_on_vacancy(self):
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"pdf")
            vacancy = Vacancy("Python Engineer", "Acme", "Python APIs")
            draft = ApplicationDraft(vacancy, "backend_python", resume, "Hello")
            ctx = SimpleNamespace(bot=FakeBot())
            with patch("bot.handlers.extract_first_url", side_effect=JobPageError("no URL")), \
                 patch("bot.handlers.build_application", return_value=draft):
                await _handle_token_free(ctx, "Python Engineer at Acme with Python APIs and PostgreSQL")
            self.assertTrue(any("Role: Python Engineer" in item["text"] for item in ctx.bot.messages))

    async def test_hirify_job_reports_clean_contact_and_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"pdf")
            vacancy = Vacancy("Python Engineer", "Acme", "Python APIs", "https://hirify.me/jobs/1-role")
            page = ParsedJobPage(vacancy, "job_page", "", vacancy.url)
            draft = ApplicationDraft(vacancy, "backend_python", resume, "old generic text")
            ctx = SimpleNamespace(bot=FakeBot())
            with patch("bot.handlers.fetch_job_from_message", new=AsyncMock(return_value=page)), \
                 patch("bot.handlers.is_hirify_job_url", return_value=True), \
                 patch("bot.handlers._get_hirify_client", return_value=FakeHirifyClient()), \
                 patch("bot.handlers.build_application_for_vacancy", return_value=draft), \
                 patch("bot.handlers.save_fetched_job", return_value="a" * 64):
                await _handle_token_free(ctx, vacancy.url)
            summary = next(item for item in ctx.bot.messages if "Contact: @brandiumsu" in item["text"])
            self.assertNotIn("Source:", summary["text"])
            self.assertNotIn("Job ID:", summary["text"])
            preview = ctx.bot.messages[-1]
            self.assertIn("Приветствую, хочу откликнуться", preview["text"])
            self.assertIsNotNone(preview["reply_markup"])


if __name__ == "__main__":
    unittest.main()
