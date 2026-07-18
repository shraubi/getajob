import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from jobbot.integrations.greenhouse import (
    fetch_greenhouse_posting,
    is_greenhouse_job_url,
    preflight_greenhouse_application,
)

URL = "https://job-boards.greenhouse.io/example/jobs/735064"


def transport():
    payload = {
        "id": 735064,
        "title": "Senior Frontend Engineer, Web3",
        "company_name": "Example",
        "location": {"name": "Remote"},
        "content": "<p>Build reliable product experiences with a cross-functional engineering team.</p>",
        "questions": [
            {"label": "First name", "required": True, "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
            {"label": "Last name", "required": True, "fields": [{"name": "last_name", "type": "input_text", "values": []}]},
            {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text", "values": []}]},
            {"label": "Resume", "required": True, "fields": [{"name": "resume", "type": "input_file", "values": []}]},
        ],
    }
    return httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))


class GreenhouseTests(unittest.TestCase):
    def test_recognizes_canonical_and_embedded_urls(self):
        self.assertTrue(is_greenhouse_job_url(URL))
        self.assertTrue(is_greenhouse_job_url(
            "https://boards.greenhouse.io/embed/job_app?for=example&token=735064"
        ))

    def test_fetches_contract_and_preflights_standard_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "resume.pdf"
            resume.write_bytes(b"pdf")
            profile = root / "applicant.json"
            profile.write_text(json.dumps({
                "first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"
            }), encoding="utf-8")
            with (
                patch("jobbot.integrations.greenhouse.validate_public_url", new=AsyncMock()),
                patch("jobbot.integrations.web_application.extract_resume_text", return_value=""),
            ):
                posting = asyncio.run(fetch_greenhouse_posting(URL, transport=transport()))
                preflight = asyncio.run(preflight_greenhouse_application(
                    URL, resume, profile, transport=transport()
                ))
        self.assertEqual(posting.page.contact_value, "greenhouse")
        self.assertEqual(preflight.missing, ())
        self.assertEqual(preflight.submissions["email"], "ada@example.com")
        self.assertEqual(preflight.submissions["resume"], "__resume__")


if __name__ == "__main__":
    unittest.main()
