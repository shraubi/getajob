import asyncio
import unittest

import httpx

from jobbot.integrations.hirify import HirifyClient, parse_contacts_response


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

    def test_detects_and_submits_direct_application_with_completed_primary_profile(self):
        requests = []

        def handler(request):
            requests.append(request)
            path = request.url.path
            if path == "/api/vacancies/732800-python-developer":
                return httpx.Response(200, json={"id": 732800, "can_apply_directly": True}, request=request)
            if path == "/sanctum/csrf-cookie":
                return httpx.Response(204, headers={"set-cookie": "XSRF-TOKEN=csrf-token; Path=/"}, request=request)
            if path == "/auth/login":
                return httpx.Response(200, json={"id": 1}, request=request)
            if path == "/api/user/applications-for-vacancy/732800":
                return httpx.Response(200, json=[], request=request)
            if path == "/auth/user":
                return httpx.Response(200, json={
                    "profile_id": 22,
                    "user_profiles": [
                        {"profile_id": 11, "status": "completed"},
                        {"profile_id": 22, "status": "completed"},
                    ],
                }, request=request)
            if path == "/api/user/applications":
                return httpx.Response(201, json={"data": {"id": 991}}, request=request)
            raise AssertionError(path)

        async def run():
            async with HirifyClient("user@example.com", "secret", transport=httpx.MockTransport(handler)) as client:
                direct = await client.get_direct_application("https://hirify.me/jobs/732800-python-developer")
                application_id = await client.apply_direct(direct.vacancy_id)
                return direct, application_id

        direct, application_id = asyncio.run(run())
        self.assertEqual(direct.vacancy_id, 732800)
        self.assertEqual(application_id, 991)
        submitted = next(request for request in requests if request.url.path == "/api/user/applications")
        self.assertEqual(submitted.headers["x-xsrf-token"], "csrf-token")
        self.assertIn(b'"profile_id":22', submitted.content)


if __name__ == "__main__":
    unittest.main()
