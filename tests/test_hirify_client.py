import asyncio
import unittest

import httpx

from hirify_client import HirifyClient, parse_contacts_response


class HirifyClientTests(unittest.TestCase):
    def test_parses_confirmed_telegram_response_without_rewriting_source_value(self):
        contact = parse_contacts_response({"contacts": [
            {"type": "telegram", "value": "brandiumsu", "short_code": "619s9"}
        ], "company_title": "Брендиум"})
        self.assertEqual((contact.value, contact.short_code), ("brandiumsu", "619s9"))
        self.assertEqual(contact.company_title, "Брендиум")

    def test_logs_in_once_before_first_contact_and_caches_slug(self):
        calls = []

        def handler(request):
            calls.append((request.method, request.url.path, request.headers.get("x-xsrf-token")))
            if request.url.path == "/sanctum/csrf-cookie":
                return httpx.Response(204, headers={"set-cookie": "XSRF-TOKEN=csrf-token; Path=/"}, request=request)
            if request.url.path == "/auth/login":
                return httpx.Response(200, json={"id": 1}, request=request)
            return httpx.Response(200, json={"contacts": [{"type": "telegram", "value": "brandiumsu"}]}, request=request)

        async def run():
            async with HirifyClient("user@example.com", "secret", transport=httpx.MockTransport(handler)) as client:
                first = await client.get_contact("https://hirify.me/jobs/732103-role")
                second = await client.get_contact("https://hirify.me/jobs/732103-role?utm_source=test")
                return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first.value, "brandiumsu")
        self.assertIs(first, second)
        self.assertIn(("GET", "/sanctum/csrf-cookie", None), calls)
        self.assertIn(("POST", "/auth/login", "csrf-token"), calls)
        self.assertNotIn(("POST", "/api/auth/login", "csrf-token"), calls)
        self.assertEqual(sum(path == "/sanctum/csrf-cookie" for _, path, _ in calls), 1)
        self.assertEqual(sum(path == "/auth/login" for _, path, _ in calls), 1)
        self.assertEqual(sum(path == "/api/vacancies/732103-role/contacts" for _, path, _ in calls), 1)

    def test_reauthenticates_only_after_expired_session(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/sanctum/csrf-cookie":
                return httpx.Response(204, headers={"set-cookie": "XSRF-TOKEN=csrf-token; Path=/"}, request=request)
            if request.url.path == "/auth/login":
                return httpx.Response(200, request=request)
            contact_calls = calls.count("/api/vacancies/732103-role/contacts")
            if contact_calls == 1:
                return httpx.Response(401, request=request)
            return httpx.Response(200, json={"contacts": [{"type": "telegram", "value": "brandiumsu"}]}, request=request)

        async def run():
            async with HirifyClient("user@example.com", "secret", transport=httpx.MockTransport(handler)) as client:
                return await client.get_contact("https://hirify.me/jobs/732103-role")

        contact = asyncio.run(run())
        self.assertEqual(contact.value, "brandiumsu")
        self.assertEqual(calls.count("/sanctum/csrf-cookie"), 2)
        self.assertEqual(calls.count("/auth/login"), 2)
        self.assertEqual(calls.count("/api/vacancies/732103-role/contacts"), 2)


if __name__ == "__main__":
    unittest.main()
