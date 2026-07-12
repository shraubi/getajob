import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from hirify_client import HirifyAuthError, HirifyClient, parse_contacts_response


class HirifyClientTests(unittest.TestCase):
    def test_parses_confirmed_telegram_response_without_rewriting_source_value(self):
        contact = parse_contacts_response({
            "contacts": [{"type": "telegram", "value": "brandiumsu", "short_code": "619s9"}],
        })
        self.assertEqual((contact.value, contact.short_code), ("brandiumsu", "619s9"))

    def test_reads_playwright_state_and_fetches_contact(self):
        seen = {}

        def handler(request):
            seen["cookie"] = request.headers.get("cookie")
            seen["xsrf"] = request.headers.get("x-xsrf-token")
            return httpx.Response(200, json={"contacts": [{"type": "telegram", "value": "brandiumsu"}]}, request=request)

        async def run(state):
            async with HirifyClient(state, transport=httpx.MockTransport(handler)) as client:
                return await client.get_contact("https://hirify.me/jobs/732103-role")

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"cookies": [
                {"name": "XSRF-TOKEN", "value": "csrf-token", "domain": ".hirify.me", "path": "/"},
                {"name": "hirifyme_session", "value": "session", "domain": ".hirify.me", "path": "/"},
            ]}), encoding="utf-8")
            contact = asyncio.run(run(state))
        self.assertEqual(contact.value, "brandiumsu")
        self.assertEqual(seen["xsrf"], "csrf-token")
        self.assertIn("hirifyme_session=session", seen["cookie"])

    def test_missing_browser_state_is_actionable(self):
        with self.assertRaisesRegex(HirifyAuthError, "auth command"):
            HirifyClient(Path("missing-state.json"))


if __name__ == "__main__":
    unittest.main()
