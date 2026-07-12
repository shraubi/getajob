"""Authenticated Hirify contact API adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx

_API_BASE = "https://api.hirify.me"
_SLUG_RE = re.compile(r"^/jobs/([^/?#]+)")


class HirifyAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class Contact:
    kind: str
    value: str
    short_code: str = ""

    @property
    def target_url(self) -> str:
        if self.kind == "telegram":
            return f"https://t.me/{self.value.lstrip('@')}"
        return self.value if self.kind == "url" else ""


def is_hirify_job_url(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.hostname or "").casefold() in {"hirify.me", "www.hirify.me"} and bool(_SLUG_RE.match(parsed.path))


def parse_contacts_response(payload: dict) -> Contact | None:
    """Keep the source value intact; Telegram formatting belongs to the sender."""
    for item in payload.get("contacts", []):
        kind = str(item.get("type", "")).casefold()
        value = str(item.get("value", "")).strip()
        short_code = str(item.get("short_code", "")).strip()
        if kind == "telegram" and re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", value):
            return Contact(kind, value.lstrip("@"), short_code)
        if kind == "url" and value.startswith(("http://", "https://")):
            return Contact(kind, value, short_code)
        if kind in {"email", "phone"} and value:
            return Contact(kind, value, short_code)
    return None


class HirifyClient:
    """Login once with a cookie jar and retry once when the session expires."""

    def __init__(
        self,
        email: str,
        password: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.email = email
        self.password = password
        self._client = httpx.AsyncClient(
            base_url=_API_BASE,
            follow_redirects=True,
            timeout=15.0,
            transport=transport,
            headers={"Accept": "application/json", "Origin": "https://hirify.me", "Referer": "https://hirify.me/"},
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self._client.aclose()

    async def login(self) -> None:
        csrf = await self._client.get("/sanctum/csrf-cookie")
        csrf.raise_for_status()
        token = self._client.cookies.get("XSRF-TOKEN")
        if not token:
            raise HirifyAuthError("Hirify did not issue a CSRF token")
        response = await self._client.post(
            "/api/auth/login",
            json={"email": self.email, "password": self.password},
            headers={"X-XSRF-TOKEN": unquote(token)},
        )
        if response.status_code in {401, 419, 422}:
            raise HirifyAuthError("Hirify login failed")
        response.raise_for_status()

    async def _request_contact(self, slug: str) -> httpx.Response:
        token = self._client.cookies.get("XSRF-TOKEN", "")
        return await self._client.post(
            f"/api/vacancies/{slug}/contacts",
            json={},
            headers={"X-XSRF-TOKEN": unquote(token)} if token else {},
        )

    async def get_contact(self, job_url: str) -> Contact | None:
        parsed = urlparse(job_url)
        match = _SLUG_RE.match(parsed.path)
        if not is_hirify_job_url(job_url) or not match:
            return None
        response = await self._request_contact(match.group(1))
        if response.status_code in {401, 419}:
            await self.login()
            response = await self._request_contact(match.group(1))
        if response.status_code in {401, 419}:
            raise HirifyAuthError("Hirify session is missing or expired")
        response.raise_for_status()
        return parse_contacts_response(response.json())
