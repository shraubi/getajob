import tempfile
import unittest
import sys
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("YOUR_CHAT_ID", "1")
sys.modules.setdefault("dotenv", SimpleNamespace(load_dotenv=lambda: None))


class InlineKeyboardButton:
    def __init__(self, text, callback_data=None):
        self.text = text
        self.callback_data = callback_data


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


telegram_module = ModuleType("telegram")
telegram_module.InlineKeyboardButton = InlineKeyboardButton
telegram_module.InlineKeyboardMarkup = InlineKeyboardMarkup
telegram_module.Update = object
telegram_ext_module = ModuleType("telegram.ext")
telegram_ext_module.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
sys.modules.setdefault("telegram", telegram_module)
sys.modules.setdefault("telegram.ext", telegram_ext_module)
storage_module = ModuleType("storage")
storage_state_module = ModuleType("storage.state")
storage_state_module.delete_pending = lambda *_: None
storage_state_module.get_pending = lambda *_: None
storage_state_module.save_pending = lambda *_: None
sys.modules.setdefault("storage", storage_module)
sys.modules.setdefault("storage.state", storage_state_module)

from jobbot import config
from jobbot.handlers import _handle_token_free, handle_callback
from jobbot.integrations.hirify import Contact, DirectApplication
from jobbot.integrations.job_page import ParsedJobPage
from jobbot.application import ApplicationDraft, Vacancy


class FakeBot:
    def __init__(self):
        self.messages = []
        self.documents = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)

    async def send_document(self, **kwargs):
        self.documents.append(kwargs)


class FakeHirifyClient:
    async def get_contact(self, _url):
        return Contact("telegram", "artem_avsievich")


class FakeDirectHirifyClient:
    applied = []

    async def get_contact(self, _url):
        return None

    async def get_direct_application(self, _url):
        return DirectApplication(732800)

    async def apply_direct(self, vacancy_id):
        self.applied.append(vacancy_id)
        return 991


class FakeSender:
    calls = []

    def __init__(self, *_args):
        pass

    async def send_resume(self, username, message, resume_path):
        self.calls.append((username, message, resume_path.name))
        return 4242


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.edited = ""

    async def answer(self):
        pass

    async def edit_message_text(self, text):
        self.edited = text


class BotApplicationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_hirify_direct_application_one_button_dry_run(self):
        url = "https://hirify.me/jobs/732800-python-developer"
        vacancy = Vacancy(
            title="Python Developer", company="BETBY",
            description="Python asyncio backend microservices role " * 5, url=url,
        )
        page = ParsedJobPage(vacancy, "structured_job_page", "", url)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "backend.pdf"
            resume.write_bytes(b"pdf")
            db_path = root / "jobs.db"
            bot = FakeBot()
            client = FakeDirectHirifyClient()
            client.applied.clear()
            draft = ApplicationDraft(vacancy, "backend_python", resume, "")
            with (
                patch("jobbot.handlers.fetch_job_from_message", new=AsyncMock(return_value=page)),
                patch("jobbot.handlers._get_hirify_client", return_value=client),
                patch("jobbot.handlers.build_application_for_vacancy", return_value=draft),
                patch.object(config, "JOBS_DB_PATH", db_path),
                patch.object(config, "RESUME_DIR", root),
            ):
                await _handle_token_free(SimpleNamespace(bot=bot), url)

            button_data = bot.messages[-1]["reply_markup"].inline_keyboard[0][0].callback_data
            self.assertTrue(button_data.startswith("hirifyapply:"))
            query = FakeQuery(button_data)
            with (
                patch("jobbot.handlers._get_hirify_client", return_value=client),
                patch.object(config, "JOBS_DB_PATH", db_path),
            ):
                await handle_callback(SimpleNamespace(callback_query=query), SimpleNamespace())
            self.assertEqual(client.applied, [732800])
            self.assertIn("Applied through Hirify", query.edited)

    async def test_external_form_one_button_submit_dry_run(self):
        url = "https://www.jobposting.pro/emploi-2640578-999"
        vacancy = Vacancy(
            title="Senior AI Engineer", company="Algoteque",
            description="AI Python LLM engineering role " * 5, url=url,
        )
        page = ParsedJobPage(vacancy, "application_form", url, url)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "ai.pdf"
            resume.write_bytes(b"pdf")
            db_path = root / "jobs.db"
            profile_path = root / "applicant.json"
            profile_path.write_text("{}", encoding="utf-8")
            bot = FakeBot()
            draft = ApplicationDraft(vacancy, "ml_engineering", resume, "")
            with (
                patch("jobbot.handlers.fetch_job_from_message", new=AsyncMock(return_value=page)),
                patch("jobbot.handlers.is_hirify_job_url", return_value=False),
                patch("jobbot.handlers.build_application_for_vacancy", return_value=draft),
                patch.object(config, "JOBS_DB_PATH", db_path),
                patch.object(config, "RESUME_DIR", root),
            ):
                await _handle_token_free(SimpleNamespace(bot=bot), url)

            preview = bot.messages[-1]
            button_data = preview["reply_markup"].inline_keyboard[0][0].callback_data
            self.assertTrue(button_data.startswith("webapply:"))
            query = FakeQuery(button_data)
            submit = AsyncMock(return_value="https://www.jobposting.pro/application/success")
            with (
                patch("jobbot.handlers.submit_application", new=submit),
                patch.object(config, "JOBS_DB_PATH", db_path),
                patch.object(config, "RESUME_DIR", root),
                patch.object(config, "APPLICATION_PROFILE_PATH", profile_path),
            ):
                await handle_callback(SimpleNamespace(callback_query=query), SimpleNamespace())
            submit.assert_awaited_once_with(url, resume, profile_path, "")
            self.assertIn("Application submitted", query.edited)

    async def test_telegram_contact_preview_and_one_button_send_dry_run(self):
        url = "https://hirify.me/jobs/732017-application-backend-engineer-python"
        vacancy = Vacancy(
            title="Application Backend Engineer (Python)",
            company="31C",
            description="Python FastAPI backend engineering role " * 5,
            url=url,
        )
        page = ParsedJobPage(vacancy, "structured_job_page", "", url)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "backend.pdf"
            resume.write_bytes(b"pdf")
            db_path = root / "jobs.db"
            bot = FakeBot()
            ctx = SimpleNamespace(bot=bot)
            draft = ApplicationDraft(vacancy, "backend_python", resume, "old generic message")

            with (
                patch("jobbot.handlers.fetch_job_from_message", new=AsyncMock(return_value=page)),
                patch("jobbot.handlers._get_hirify_client", return_value=FakeHirifyClient()),
                patch("jobbot.handlers.build_application_for_vacancy", return_value=draft),
                patch.object(config, "JOBS_DB_PATH", db_path),
                patch.object(config, "RESUME_DIR", root),
            ):
                await _handle_token_free(ctx, url)

            summary = bot.messages[1]["text"]
            self.assertNotIn("Job ID", summary)
            self.assertNotIn("Source:", summary)
            self.assertIn("Contact: @artem_avsievich", summary)
            self.assertEqual(len(bot.documents), 1)

            preview = bot.messages[2]
            self.assertIn("Приветствую, хочу откликнуться", preview["text"])
            self.assertIn(url, preview["text"])
            self.assertNotIn(f'"{url}"', preview["text"])
            button_data = preview["reply_markup"].inline_keyboard[0][0].callback_data
            self.assertTrue(button_data.startswith("apply:"))

            query = FakeQuery(button_data)
            update = SimpleNamespace(callback_query=query)
            FakeSender.calls.clear()
            with (
                patch("jobbot.handlers.TelegramSender", FakeSender),
                patch.object(config, "JOBS_DB_PATH", db_path),
                patch.object(config, "RESUME_DIR", root),
                patch.object(config, "TELEGRAM_API_ID", 1),
                patch.object(config, "TELEGRAM_API_HASH", "hash"),
            ):
                await handle_callback(update, SimpleNamespace())

            self.assertEqual(FakeSender.calls[0][0], "artem_avsievich")
            self.assertEqual(FakeSender.calls[0][2], "backend.pdf")
            self.assertIn("Приветствую, хочу откликнуться", FakeSender.calls[0][1])
            self.assertIn("Sent to @artem_avsievich", query.edited)

            duplicate_query = FakeQuery(button_data)
            with (
                patch("jobbot.handlers.TelegramSender", FakeSender),
                patch.object(config, "JOBS_DB_PATH", db_path),
            ):
                await handle_callback(SimpleNamespace(callback_query=duplicate_query), SimpleNamespace())
            self.assertEqual(len(FakeSender.calls), 1)
            self.assertIn("already sending or sent", duplicate_query.edited)


if __name__ == "__main__":
    unittest.main()
