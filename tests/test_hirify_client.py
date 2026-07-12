import asyncio
import unittest

import httpx

from hirify_client import HirifyClient, parse_contacts_response


class HirifyClientTests(unittest.TestCase):
    def test_parses_confirmed_telegram_response_without_rewriting_source_value(self):
        contact = parse_contacts_response({
            "vacancy_id": "732103",
            "contacts": [{"type": "telegram", "value": "brandiumsu", "short_code": "619s9"}],
            "company_title": "Brandium",
            "access_type": "free_vacancy",
        })
        self.assertEqual(contact.value, "brandiumsu")
        self.assertEqual(contact.short_code, "619s9")
        self.assertEqual(contact.target_url, "https://t.me/brandiumsu")

    def test_parses_confirmed_external_url_response(self):
        contact = parse_contacts_response({
            "vacancy_id": "471829",
            "contacts": [{"type": "url", "value": "https://www.appxite.com/jobs/api", "short_code": "lSRV"}],
        })
        self.assertEqual(contact.target_url, "https://www.appxite.com/jobs/api")

    def test_reauthenticates_and_retries_contact_request(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/api/vacancies/732103-role/contacts" and calls.count(request.url.path) == 1:
                return httpx.Response(401, request=request)
            if request.url.path == "/sanctum/csrf-cookie":
                return httpx.Response(204, headers={"set-cookie": "XSRF-TOKEN=csrf-token; Path=/"}, request=request)
            if request.url.path == "/api/auth/login":
                return httpx.Response(200, json={"id": 1}, request=request)
            return httpx.Response(200, json={"contacts": [{"type": "telegram", "value": "brandiumsu"}]}, request=request)

        async def run():
            async with HirifyClient("user@example.com", "secret", transport=httpx.MockTransport(handler)) as client:
                return await client.get_contact("https://hirify.me/jobs/732103-role")

        contact = asyncio.run(run())
        self.assertEqual(contact.value, "brandiumsu")
        self.assertEqual(calls.count("/api/vacancies/732103-role/contacts"), 2)
        self.assertIn("/api/auth/login", calls)


if __name__ == "__main__":
    unittest.main()
