import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from jobbot.integrations.ashby import (
    AshbyError,
    AshbySubmissionResult,
    _diagnostic_text,
    _diagnostic_url,
    _recaptcha_requires_user,
    _resolve_submit_control,
    fetch_ashby_posting,
    preflight_ashby_application,
    submit_ashby_application,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ashby_clipboard_job.json"
CLIPBOARD_URL = "https://jobs.ashbyhq.com/clipboard/d77b2224-307f-48b1-a0ea-ab67153993c0"


def fixture_transport(payload=None):
    if payload is None:
        payload = FIXTURE_PATH.read_text(encoding="utf-8")
    elif not isinstance(payload, str):
        payload = json.dumps(payload)

    def handler(request):
        if request.method != "POST":
            return httpx.Response(405, request=request)
        return httpx.Response(
            200,
            text=payload,
            headers={"content-type": "application/json"},
            request=request,
        )

    return httpx.MockTransport(handler)


class AshbyTests(unittest.TestCase):
    def test_parses_clipboard_vacancy_and_form_contract(self):
        with patch("jobbot.integrations.ashby.validate_public_url", new=AsyncMock()):
            posting = asyncio.run(
                fetch_ashby_posting(CLIPBOARD_URL + "/application", transport=fixture_transport())
            )
        self.assertEqual(posting.page.vacancy.title, "Technical Support Engineer")
        self.assertEqual(posting.page.vacancy.company, "Clipboard")
        self.assertEqual(posting.page.vacancy.location, "Remote (Non-U.S.)")
        self.assertEqual(posting.page.contact_kind, "ashby")
        self.assertEqual(posting.page.apply_url, CLIPBOARD_URL + "/application")
        self.assertIn("_systemfield_resume", {field.path for field in posting.fields})

    def test_preflight_maps_standard_fields_and_explicit_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "resume.pdf"
            resume.write_bytes(b"pdf")
            profile = root / "applicant.json"
            profile.write_text(
                json.dumps(
                    {
                        "first_name": "Ada",
                        "last_name": "Lovelace",
                        "email": "ada@example.com",
                        "location": {"country": "Exampleland", "city": "Paris"},
                        "facts": {
                            "work_authorized_countries": ["Exampleland"],
                            "previous_employers": [],
                            "application_source_preferences": ["LinkedIn", "Indeed", "Other"]
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("jobbot.integrations.ashby.validate_public_url", new=AsyncMock()),
                patch("jobbot.integrations.web_application.extract_resume_text", return_value=""),
            ):
                preflight = asyncio.run(
                    preflight_ashby_application(
                        CLIPBOARD_URL,
                        resume,
                        profile,
                        transport=fixture_transport(),
                    )
                )
        self.assertEqual(preflight.missing, ())
        self.assertEqual(preflight.submissions["_systemfield_name"], "Ada Lovelace")
        self.assertEqual(preflight.submissions["_systemfield_resume"], "__resume__")
        self.assertEqual(preflight.submissions["clipboard_work_authorization"], True)
        self.assertEqual(preflight.submissions["clipboard_previously_worked"], False)
        self.assertEqual(preflight.submissions["clipboard_source"], "LinkedIn")

    def test_semantic_facts_survive_different_question_ids_and_wording(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        entries = payload["data"]["jobPosting"]["applicationForm"]["sections"][0]["fieldEntries"]
        replacements = {
            "clipboard_service_country": ("new-location-id", "Where will you be working from?"),
            "clipboard_work_authorization": (
                "new-authorization-id",
                "Do you have the legal right to work in your current country?"
            ),
            "clipboard_previously_worked": (
                "new-employment-id",
                "Were you previously employed by Clipboard?"
            ),
            "clipboard_source": (
                "new-source-id",
                "Where did you hear about this opportunity?"
            ),
        }
        for entry in entries:
            path = entry["field"]["path"]
            if path in replacements:
                entry["field"]["path"], entry["field"]["title"] = replacements[path]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "resume.pdf"
            resume.write_bytes(b"pdf")
            profile = root / "applicant.json"
            profile.write_text(json.dumps({
                "first_name": "Ada",
                "last_name": "Lovelace",
                "email": "ada@example.com",
                "location": {"country": "Exampleland"},
                "facts": {
                    "work_authorized_countries": ["Exampleland"],
                    "previous_employers": [],
                    "application_source_preferences": ["LinkedIn", "Indeed", "Other"]
                }
            }), encoding="utf-8")
            with (
                patch("jobbot.integrations.ashby.validate_public_url", new=AsyncMock()),
                patch("jobbot.integrations.web_application.extract_resume_text", return_value=""),
            ):
                preflight = asyncio.run(preflight_ashby_application(
                    CLIPBOARD_URL, resume, profile, transport=fixture_transport(payload)
                ))
        self.assertEqual(preflight.missing, ())
        self.assertEqual(preflight.submissions["new-authorization-id"], True)
        self.assertEqual(preflight.submissions["new-employment-id"], False)
        self.assertEqual(preflight.submissions["new-source-id"], "LinkedIn")

    def test_preflight_returns_actionable_missing_questions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "resume.pdf"
            resume.write_bytes(b"pdf")
            profile = root / "applicant.json"
            profile.write_text(
                json.dumps(
                    {
                        "first_name": "Ada",
                        "last_name": "Lovelace",
                        "email": "ada@example.com"
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("jobbot.integrations.ashby.validate_public_url", new=AsyncMock()),
                patch("jobbot.integrations.web_application.extract_resume_text", return_value=""),
            ):
                preflight = asyncio.run(
                    preflight_ashby_application(
                        CLIPBOARD_URL,
                        resume,
                        profile,
                        transport=fixture_transport(),
                    )
                )
        missing = "\n".join(preflight.missing)
        self.assertIn("authorized to work", missing)
        self.assertIn("clipboard_work_authorization", missing)
        self.assertIn("How did you hear", missing)


    def test_submission_diagnostics_are_bounded_and_redacted(self):
        diagnostic = _diagnostic_text(
            "Email ada@example.com token=super-secret " + ("x" * 500),
            limit=100,
        )
        self.assertIn("[email]", diagnostic)
        self.assertIn("token=[redacted]", diagnostic)
        self.assertNotIn("ada@example.com", diagnostic)
        self.assertNotIn("super-secret", diagnostic)
        self.assertLessEqual(len(diagnostic), 100)

    def test_diagnostic_url_drops_query_and_fragment(self):
        self.assertEqual(
            _diagnostic_url(
                CLIPBOARD_URL + "/application?token=secret#private"
            ),
            CLIPBOARD_URL + "/application",
        )

    def test_attached_recaptcha_requires_user_even_when_not_visible(self):
        self.assertTrue(_recaptcha_requires_user(
            control_present=True,
            challenge_visible=False,
            token_present=False,
        ))
        self.assertFalse(_recaptcha_requires_user(
            control_present=True,
            challenge_visible=False,
            token_present=True,
        ))


    def test_submit_control_is_re_resolved_by_semantic_name_not_live_index(self):
        class DynamicCandidates:
            async def evaluate_all(self, _script):
                return ["Upload file", "Submit Application"]

            def nth(self, _index):
                raise AssertionError("live DOM indices must not be clicked")

        class StableSubmit:
            async def count(self):
                return 1

        stable_submit = StableSubmit()

        class FakePage:
            def locator(self, _selector):
                return DynamicCandidates()

            def get_by_role(self, role, *, name, exact):
                self.lookup = (role, name, exact)
                return stable_submit

        page = FakePage()
        control, name, labels = asyncio.run(
            _resolve_submit_control(page)
        )
        self.assertIs(control, stable_submit)
        self.assertEqual(name, "Submit Application")
        self.assertEqual(labels, ["Upload file", "Submit Application"])
        self.assertEqual(
            page.lookup,
            ("button", "Submit Application", True),
        )

    def test_success_and_validation_failure_are_not_conflated(self):
        async def accepted(_preflight, _resume, _profile_dir, _headless):
            return AshbySubmissionResult(
                "submitted", CLIPBOARD_URL + "/application/success", "confirmed"
            )

        async def rejected(_preflight, _resume, _profile_dir, _headless):
            return AshbySubmissionResult(
                "failed", CLIPBOARD_URL + "/application", "Email is invalid"
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "resume.pdf"
            resume.write_bytes(b"pdf")
            profile = root / "applicant.json"
            profile.write_text(
                json.dumps(
                    {
                        "first_name": "Ada",
                        "last_name": "Lovelace",
                        "email": "ada@example.com",
                        "location": {"country": "Exampleland"},
                        "facts": {
                            "work_authorized_countries": ["Exampleland"],
                            "previous_employers": [],
                            "application_source_preferences": ["LinkedIn", "Indeed", "Other"]
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("jobbot.integrations.ashby.validate_public_url", new=AsyncMock()),
                patch("jobbot.integrations.web_application.extract_resume_text", return_value=""),
                patch(
                    "jobbot.integrations.ashby.fetch_ashby_posting",
                    new=fetch_fixture_posting,
                ),
            ):
                success = asyncio.run(
                    submit_ashby_application(
                        CLIPBOARD_URL, resume, profile, root / "browser",
                        browser_submitter=accepted,
                    )
                )
                failure = asyncio.run(
                    submit_ashby_application(
                        CLIPBOARD_URL, resume, profile, root / "browser",
                        browser_submitter=rejected,
                    )
                )
        self.assertEqual(success.status, "submitted")
        self.assertEqual(failure.status, "failed")
        self.assertIn("Email", failure.detail)


async def fetch_fixture_posting(*_args, **_kwargs):
    with patch("jobbot.integrations.ashby.validate_public_url", new=AsyncMock()):
        return await fetch_ashby_posting(CLIPBOARD_URL, transport=fixture_transport())


@unittest.skipUnless(os.environ.get("ASHBY_LIVE_SMOKE_URL"), "opt-in Ashby live smoke")
class AshbyLiveSmokeTests(unittest.TestCase):
    def test_public_contract_is_readable(self):
        posting = asyncio.run(fetch_ashby_posting(os.environ["ASHBY_LIVE_SMOKE_URL"]))
        self.assertTrue(posting.fields)
        self.assertTrue(posting.page.apply_url.endswith("/application"))


if __name__ == "__main__":
    unittest.main()
