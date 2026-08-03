import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from jobbot.integrations.hellowork import (
    HelloWorkError,
    HelloWorkSubmissionResult,
    _application_already_recorded,
    check_requirements,
    fetch_hellowork_posting,
    parse_hellowork_url,
    preflight_hellowork_application,
    submit_hellowork_account_application,
)

URL = "https://www.hellowork.com/fr-fr/emplois/81835625.html"
HTML = """
<html><head><script type="application/ld+json">
{"@type":"JobPosting","title":"Aide MÃ©nager H/F","description":"MÃ©nage Ã  domicile auprÃ¨s de particuliers avec entretien complet du logement et accompagnement quotidien.",
 "hiringOrganization":{"name":"Amelis"},"jobLocation":{"address":{"addressLocality":"Paris"}}}
</script></head><body><a href="#apply">Postuler</a></body></html>
"""


class HelloWorkTests(unittest.IsolatedAsyncioTestCase):
    async def test_detects_accented_success_and_already_applied_text(self):
        self.assertTrue(_application_already_recorded("Candidature envoyÃ©e"))
        self.assertTrue(_application_already_recorded("Vous avez dÃ©jÃ  postulÃ©"))
        self.assertFalse(_application_already_recorded("Envoyer ma candidature"))

    async def test_parses_canonical_offer(self):
        self.assertEqual(parse_hellowork_url(URL)[0], "81835625")
        with self.assertRaises(HelloWorkError):
            parse_hellowork_url("https://example.com/fr-fr/emplois/81835625.html")

    async def test_fetches_structured_offer(self):
        def handler(request):
            return httpx.Response(200, text=HTML, request=request)

        with patch("jobbot.integrations.hellowork.validate_public_url", new=AsyncMock()):
            posting = await fetch_hellowork_posting(URL, transport=httpx.MockTransport(handler))
        self.assertEqual(posting.offer_id, "81835625")
        self.assertEqual(posting.page.vacancy.company, "Amelis")
        self.assertEqual(posting.page.contact_value, "hellowork")

    async def test_mandatory_requirements_use_explicit_profile_facts(self):
        description = "Le CACES R489 est obligatoire. Le permis B est souhaitÃ©."
        status, details = check_requirements(
            description, {"facts": {"missing_requirements": ["CACES R489"]}}
        )
        self.assertEqual(status, "requirements_unmet")
        self.assertEqual(len(details), 1)
        status, _ = check_requirements(
            description, {"facts": {"qualifications": ["CACES R489"]}}
        )
        self.assertEqual(status, "ok")

    async def test_preflight_pauses_ambiguous_mandatory_qualification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "resume.pdf"
            resume.write_bytes(b"pdf")
            profile = root / "applicant.json"
            profile.write_text("{}", encoding="utf-8")
            posting = await self._posting_with_description("Le permis B est obligatoire.")
            with patch(
                "jobbot.integrations.hellowork.fetch_hellowork_posting",
                new=AsyncMock(return_value=posting),
            ):
                with self.assertRaises(HelloWorkError) as raised:
                    await preflight_hellowork_application(URL, resume, profile)
            self.assertEqual(raised.exception.status, "requirements_ambiguous")

    async def test_account_application_is_exactly_two_clicks_without_local_resume(self):
        clicks = []

        class Locator:
            def __init__(self, kind, page=None):
                self.kind = kind
                self.page = page
                self.first = self
                self.last = self

            def or_(self, other):
                return self

            async def count(self):
                return 0 if self.kind in {"password", "challenge"} else 1

            async def click(self):
                clicks.append(self.kind)
                if self.page:
                    self.page.stage += 1

            async def inner_text(self):
                return (
                    "Merci pour votre candidature"
                    if self.page.stage >= 2 else "Offre HelloWork"
                )

        class Page:
            def __init__(self):
                self.url = URL
                self.stage = 0
                self.role_calls = 0

            async def goto(self, url, **kwargs):
                self.url = url

            async def wait_for_timeout(self, milliseconds):
                return None

            def locator(self, selector):
                if selector == "body":
                    return Locator("body", self)
                if "password" in selector:
                    return Locator("password", self)
                return Locator("challenge", self)

            def get_by_role(self, role, name=None):
                self.role_calls += 1
                if self.role_calls <= 2:
                    return Locator("apply", self)
                return Locator("confirm", self)

        page = Page()

        class Context:
            async def new_page(self):
                return page

            async def storage_state(self, **kwargs):
                return None

        class Browser:
            async def new_context(self, **kwargs):
                return Context()

            async def close(self):
                return None

        class Chromium:
            async def launch(self, **kwargs):
                return Browser()

        class Playwright:
            chromium = Chromium()

        class Manager:
            async def __aenter__(self):
                return Playwright()

            async def __aexit__(self, *args):
                return None

        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "hellowork-auth.json"
            auth.write_text("{}", encoding="utf-8")
            with patch("playwright.async_api.async_playwright", return_value=Manager()):
                result = await submit_hellowork_account_application(URL, auth)

        self.assertEqual(
            result,
            HelloWorkSubmissionResult(
                "submitted", URL,
                "application_marker=1 confirm_controls=1 submit_controls=0",
            ),
        )
        self.assertEqual(clicks, ["apply", "confirm"])

    async def _posting_with_description(self, description):
        payload = HTML.replace(
            "MÃ©nage Ã  domicile auprÃ¨s de particuliers avec entretien complet du logement et accompagnement quotidien.",
            description + " ExpÃ©rience en entretien de logements auprÃ¨s de particuliers et accompagnement quotidien.",
        )
        def handler(request):
            return httpx.Response(200, text=payload, request=request)
        with patch("jobbot.integrations.hellowork.validate_public_url", new=AsyncMock()):
            return await fetch_hellowork_posting(URL, transport=httpx.MockTransport(handler))


if __name__ == "__main__":
    unittest.main()

