import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from jobbot.integrations.hellowork import (
    HelloWorkError,
    check_requirements,
    fetch_hellowork_posting,
    parse_hellowork_url,
    preflight_hellowork_application,
)

URL = "https://www.hellowork.com/fr-fr/emplois/81835625.html"
HTML = """
<html><head><script type="application/ld+json">
{"@type":"JobPosting","title":"Aide Ménager H/F","description":"Ménage à domicile auprès de particuliers avec entretien complet du logement et accompagnement quotidien.",
 "hiringOrganization":{"name":"Amelis"},"jobLocation":{"address":{"addressLocality":"Paris"}}}
</script></head><body><a href="#apply">Postuler</a></body></html>
"""


class HelloWorkTests(unittest.IsolatedAsyncioTestCase):
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
        description = "Le CACES R489 est obligatoire. Le permis B est souhaité."
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

    async def _posting_with_description(self, description):
        payload = HTML.replace(
            "Ménage à domicile auprès de particuliers avec entretien complet du logement et accompagnement quotidien.",
            description + " Expérience en entretien de logements auprès de particuliers et accompagnement quotidien.",
        )
        def handler(request):
            return httpx.Response(200, text=payload, request=request)
        with patch("jobbot.integrations.hellowork.validate_public_url", new=AsyncMock()):
            return await fetch_hellowork_posting(URL, transport=httpx.MockTransport(handler))


if __name__ == "__main__":
    unittest.main()
