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
    def __init__(self, text, callback_data=None, url=None):
        self.text = text
        self.callback_data = callback_data
        self.url = url


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
from jobbot.handlers import _handle_token_free, _try_handle_answer_message, handle_callback
from jobbot.form_answers import FormQuestion, create_answer_batch, set_batch_message_id
from jobbot.store import get_job, save_fetched_job
from jobbot.telegram_queue import process_telegram_queue_once
from jobbot.integrations.ats import AtsPreflight, AtsSubmissionResult
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


class FakeEmailHirifyClient:
    async def get_contact(self, _url):
        return Contact("email", "jobs@example.com")


class FakeUrlHirifyClient:
    async def get_contact(self, _url):
        return Contact("url", "https://job-boards.greenhouse.io/example/jobs/735064")


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
    async def test_numbered_answer_batch_persists_and_submits_immediately(self):
        url = "https://jobs.ashbyhq.com/example/11111111-1111-1111-1111-111111111111"
        vacancy = Vacancy(
            title="Python Engineer", company="Example",
            description="Python backend engineering role " * 5, url=url,
        )
        page = ParsedJobPage(
            vacancy, "ashby_application_form", url + "/application", url,
            contact_kind="ats", contact_value="ashby",
        )
        question = FormQuestion(
            "ashby", "sponsorship", "Will you require sponsorship?", "Boolean",
            canonical_fact="work.requires_sponsorship_now",
            scope_type="job", scope_value="111",
            sensitive=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "backend.pdf"
            resume.write_bytes(b"pdf")
            profile = root / "applicant.json"
            profile.write_text("{}", encoding="utf-8")
            db_path = root / "jobs.db"
            job_id = save_fetched_job(db_path, page, "backend_python", resume.name)
            batch_id = create_answer_batch(db_path, job_id, 1, (question,))
            set_batch_message_id(db_path, batch_id, 77)
            bot = FakeBot()
            message = SimpleNamespace(
                text="1. No", caption=None,
                chat=SimpleNamespace(id=1),
                reply_to_message=SimpleNamespace(message_id=77),
            )
            result = AtsSubmissionResult("submitted", url + "/application/success", "ok")
            with (
                patch("jobbot.handlers._preflight_saved_job", new=AsyncMock(return_value=())),
                patch("jobbot.handlers.submit_ats_application", new=AsyncMock(return_value=result)) as submit,
                patch.object(config, "JOBS_DB_PATH", db_path),
                patch.object(config, "RESUME_DIR", root),
                patch.object(config, "APPLICATION_PROFILE_PATH", profile),
                patch.object(config, "ASHBY_BROWSER_PROFILE_PATH", root / "browser"),
            ):
                handled = await _try_handle_answer_message(
                    SimpleNamespace(message=message), SimpleNamespace(bot=bot)
                )
            self.assertTrue(handled)
            submit.assert_awaited_once()
            self.assertEqual(get_job(db_path, job_id)["status"], "sent")
            self.assertIn("Application submitted", bot.messages[-1]["text"])

    async def test_email_contact_has_action_button(self):
        url = "https://hirify.me/jobs/740343-ai-engineer-applied-ai-engineer-python"
        vacancy = Vacancy(
            title="AI Engineer", company="Example",
            description="AI engineer Python LLM role " * 5, url=url,
        )
        page = ParsedJobPage(vacancy, "structured_job_page", "", url)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "ai.pdf"
            resume.write_bytes(b"pdf")
            bot = FakeBot()
            draft = ApplicationDraft(vacancy, "ml_engineering", resume, "")
            with (
                patch("jobbot.handlers.fetch_job_from_message", new=AsyncMock(return_value=page)),
                patch("jobbot.handlers._get_hirify_client", return_value=FakeEmailHirifyClient()),
                patch("jobbot.handlers.build_application_for_vacancy", return_value=draft),
                patch.object(config, "JOBS_DB_PATH", root / "jobs.db"),
                patch.object(config, "RESUME_DIR", root),
            ):
                await _handle_token_free(SimpleNamespace(bot=bot), url)
        button = bot.messages[-1]["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.url, "mailto:jobs@example.com")
        self.assertIn("Contact: jobs@example.com", bot.messages[1]["text"])

    async def test_ats_target_preserves_hirify_vacancy_for_classification(self):
        url = "https://hirify.me/jobs/735064-senior-frontend-engineer-web3"
        source_vacancy = Vacancy(
            title="Senior Frontend Engineer Web3", company="Example",
            description="Python backend platform engineering role " * 5, url=url,
        )
        source_page = ParsedJobPage(source_vacancy, "structured_job_page", "", url)
        target_url = "https://job-boards.greenhouse.io/example/jobs/735064"
        target_vacancy = Vacancy(
            title="Sr Software Engineer, Front End", company="Example",
            description="Frontend product role " * 5, url=target_url,
        )
        ats_page = ParsedJobPage(
            target_vacancy, "greenhouse_application_form", target_url, target_url,
            contact_kind="ats", contact_value="greenhouse",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "backend.pdf"
            resume.write_bytes(b"pdf")
            bot = FakeBot()
            draft = ApplicationDraft(source_vacancy, "backend_python", resume, "")
            builder = unittest.mock.Mock(return_value=draft)
            preflight = AtsPreflight("greenhouse", ats_page, (), object())
            with (
                patch("jobbot.handlers.fetch_job_from_message", new=AsyncMock(return_value=source_page)),
                patch("jobbot.handlers._get_hirify_client", return_value=FakeUrlHirifyClient()),
                patch("jobbot.handlers.resolve_application_url", new=AsyncMock(return_value=target_url)),
                patch("jobbot.handlers.fetch_ats_page", new=AsyncMock(return_value=ats_page)),
                patch("jobbot.handlers.preflight_ats_application", new=AsyncMock(return_value=preflight)),
                patch("jobbot.handlers.build_application_for_vacancy", builder),
                patch.object(config, "JOBS_DB_PATH", root / "jobs.db"),
                patch.object(config, "RESUME_DIR", root),
                patch.object(config, "APPLICATION_PROFILE_PATH", root / "applicant.json"),
            ):
                await _handle_token_free(SimpleNamespace(bot=bot), url)
        self.assertIs(builder.call_args.args[0], source_vacancy)
        self.assertTrue(
            bot.messages[-1]["reply_markup"].inline_keyboard[0][0].callback_data.startswith("atsapply:")
        )

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

    async def test_ashby_duplicate_callback_submits_once(self):
        url = "https://jobs.ashbyhq.com/clipboard/d77b2224-307f-48b1-a0ea-ab67153993c0"
        vacancy = Vacancy(
            title="Technical Support Engineer",
            company="Clipboard",
            description="Python support engineering troubleshooting role " * 5,
            url=url,
        )
        page = ParsedJobPage(
            vacancy, "ashby_application_form", url + "/application", url,
            contact_kind="ats", contact_value="ashby",
        )
        preflight = AtsPreflight("ashby", page, (), object())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "backend.pdf"
            resume.write_bytes(b"pdf")
            profile_path = root / "applicant.json"
            profile_path.write_text("{}", encoding="utf-8")
            db_path = root / "jobs.db"
            bot = FakeBot()
            draft = ApplicationDraft(vacancy, "backend_python", resume, "")
            with (
                patch("jobbot.handlers.fetch_ats_page", new=AsyncMock(return_value=page)),
                patch("jobbot.handlers.preflight_ats_application", new=AsyncMock(return_value=preflight)),
                patch("jobbot.handlers.build_application_for_vacancy", return_value=draft),
                patch.object(config, "JOBS_DB_PATH", db_path),
                patch.object(config, "RESUME_DIR", root),
                patch.object(config, "APPLICATION_PROFILE_PATH", profile_path),
            ):
                await _handle_token_free(SimpleNamespace(bot=bot), url)

            button_data = bot.messages[-1]["reply_markup"].inline_keyboard[0][0].callback_data
            self.assertTrue(button_data.startswith("atsapply:"))
            submit = AsyncMock(return_value=AtsSubmissionResult(
                "submitted", url + "/application/success", "confirmed"
            ))
            with (
                patch("jobbot.handlers.submit_ats_application", new=submit),
                patch.object(config, "JOBS_DB_PATH", db_path),
                patch.object(config, "RESUME_DIR", root),
                patch.object(config, "APPLICATION_PROFILE_PATH", profile_path),
                patch.object(config, "ASHBY_BROWSER_PROFILE_PATH", root / "browser"),
                patch.object(config, "ATS_BROWSER_HEADLESS", True),
            ):
                first = FakeQuery(button_data)
                await handle_callback(SimpleNamespace(callback_query=first), SimpleNamespace())
                duplicate = FakeQuery(button_data)
                await handle_callback(SimpleNamespace(callback_query=duplicate), SimpleNamespace())

            submit.assert_awaited_once()
            self.assertIn("Application submitted", first.edited)
            self.assertIn("already sending or sent", duplicate.edited)

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
                patch.object(config, "JOBS_DB_PATH", db_path),
                patch.object(config, "RESUME_DIR", root),
            ):
                await handle_callback(update, SimpleNamespace())

            self.assertEqual(FakeSender.calls, [])
            self.assertIn("Queued for Telegram delivery", query.edited)
            with (
                patch("jobbot.telegram_queue.TelegramSender", FakeSender),
                patch.object(config, "JOBS_DB_PATH", db_path),
                patch.object(config, "RESUME_DIR", root),
                patch.object(config, "TELEGRAM_API_ID", 1),
                patch.object(config, "TELEGRAM_API_HASH", "hash"),
                patch.object(config, "TELEGRAM_SEND_MIN_INTERVAL_SECONDS", 0),
                patch.object(config, "TELEGRAM_SEND_MAX_PER_HOUR", 0),
            ):
                self.assertTrue(await process_telegram_queue_once())

            self.assertEqual(FakeSender.calls[0][0], "artem_avsievich")
            self.assertEqual(FakeSender.calls[0][2], "backend.pdf")
            self.assertIn("Приветствую, хочу откликнуться", FakeSender.calls[0][1])

            duplicate_query = FakeQuery(button_data)
            with (
                patch.object(config, "JOBS_DB_PATH", db_path),
            ):
                await handle_callback(SimpleNamespace(callback_query=duplicate_query), SimpleNamespace())
            self.assertEqual(len(FakeSender.calls), 1)
            self.assertIn("already queued, sending, or sent", duplicate_query.edited)


if __name__ == "__main__":
    unittest.main()

