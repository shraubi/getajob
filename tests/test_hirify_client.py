import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from hirify_client import HirifyClient, parse_contacts_response


def write_state(path: Path) -> None:
    path.write_text(json.dumps({"cookies": [
        {"name": "XSRF-TOKEN", "value": "csrf-token", "domain": ".hirify.me", "path": "/"},
        {"name": "hirifyme_session", "value": "session", "domain": ".hirify.me", "path": "/"},
    ]}), encoding="utf-8")


class HirifyClientTests(unittest.TestCase):
    def test_parses_confirmed_telegram_response_without_rewriting_source_value(self):
        contact = parse_contacts_response({"contacts": [
            {"type": "telegram", "value": "brandiumsu", "short_code": "619s9"}
        ]})
        self.assertEqual((contact.value, contact.short_code), ("brandiumsu", "619s9"))

    def test_missing_state_triggers_browser_login_inside_client(self):
        logins = []

        async def login(email, password, state_path, executable):
            logins.append((email, password, executable))
            write_state(state_path)

        def handler(request):
            return httpx.Response(200, json={"contacts": [{"type": "telegram", "value": "brandiumsu"}]}, request=request)

        async def run(state):
            async with HirifyClient("user@example.com", "secret", state, transport=httpx.MockTransport(handler), login=login) as client:
                return await client.get_contact("https://hirify.me/jobs/732103-role")

        with tempfile.TemporaryDirectory() as directory:
            contact = asyncio.run(run(Path(directory) / "state.json"))
        self.assertEqual(contact.value, "brandiumsu")
        self.assertEqual(logins, [("user@example.com", "secret", "/usr/bin/chromium")])

    def test_unauthorized_contact_relogs_and_retries_once(self):
        calls = 0
        logins = 0

        async def login(_email, _password, state_path, _executable):
            nonlocal logins
            logins += 1
            write_state(state_path)

        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(401, request=request)
            return httpx.Response(200, json={"contacts": [{"type": "telegram", "value": "brandiumsu"}]}, request=request)

        async def run(state):
            async with HirifyClient("user@example.com", "secret", state, transport=httpx.MockTransport(handler), login=login) as client:
                return await client.get_contact("https://hirify.me/jobs/732103-role")

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            write_state(state)
            contact = asyncio.run(run(state))
        self.assertEqual(contact.value, "brandiumsu")
        self.assertEqual((calls, logins), (2, 1))


if __name__ == "__main__":
    unittest.main()
