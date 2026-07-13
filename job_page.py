"""Safe, source-agnostic job page fetching and parsing."""

import asyncio
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from token_free import Vacancy

_URL_RE = re.compile(r"https?://[^\s<>\]]+")
_ACTION_RE = re.compile(
    r"\b(apply|application|submit|contact|email|send\s+(?:cv|resume)|"
    r"\u043e\u0442\u043a\u043b\u0438\u043a|\u043e\u0442\u043a\u043b\u0438\u043a\u043d\u0443\u0442\u044c\u0441\u044f|"
    r"\u0441\u0432\u044f\u0437\u0430\u0442\u044c\u0441\u044f|\u043d\u0430\u043f\u0438\u0441\u0430\u0442\u044c|"
    r"\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c\s+\u0440\u0435\u0437\u044e\u043c\u0435|\u043f\u043e\u0434\u0430\u0442\u044c\s+\u0437\u0430\u044f\u0432\u043a\u0443)\b",
    re.I,
)
_MAX_BYTES = 2_000_000
_MAX_REDIRECTS = 4
_MAX_APPLICATION_HOPS = 3


class JobPageError(RuntimeError):
    pass


class UnsafeUrlError(JobPageError):
    pass


class PageUnavailableError(JobPageError):
    pass


@dataclass(frozen=True)
class ParsedJobPage:
    vacancy: Vacancy
    source_category: str
    apply_url: str
    fetched_url: str
    contact_kind: str = ""
    contact_value: str = ""


def extract_first_url(message: str) -> str:
    match = _URL_RE.search(message)
    if not match:
        raise JobPageError("No public job URL found in the message")
    return match.group(0).rstrip(".,);]")


def _resolved_ips(host: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve job page host: {host}") from exc


async def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeUrlError("Only public HTTP(S) job URLs are allowed")
    ips = await asyncio.to_thread(_resolved_ips, parsed.hostname)
    if not ips or any(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        for ip in ips
    ):
        raise UnsafeUrlError("Job URL resolves to a non-public network address")


async def fetch_html(url: str, transport: httpx.AsyncBaseTransport | None = None) -> tuple[str, str]:
    current = url
    headers = {"User-Agent": "getajob/1.0 (+public job page parser)", "Accept": "text/html,application/xhtml+xml"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), headers=headers, follow_redirects=False, transport=transport) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            await validate_public_url(current)
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise JobPageError("Redirect response did not…14144 tokens truncated…lf.assertEqual(first.value, "brandiumsu")
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
