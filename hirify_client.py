"""Authenticated Hirify contacts adapter using Laravel Sanctum SPA auth."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx

_API_BASE = "https://api.hirify.me"
_SLUG_RE = re.compile(r"^/jobs/([^/?#]+)")


class HirifyError(RuntimeError):
    pass


class HirifyAuthError(HirifyError):
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
    def __init__(self, email: str, password: str, *, transport: httpx.AsyncBaseTransport | None = None):
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
        if csrf.is_error:
            raise HirifyAuthError(f"Hirify CSRF request failed (HTTP {csrf.status_code})")
        token = self._client.cookies.get("XSRF-TOKEN")
        if not token:
            raise HirifyAuthError("Hirify did not issue an XSRF token")
        response = await self._client.post(
            "/auth/login",
            json={"email": self.email, "password": self.password},
            headers={"X-XSRF-TOKEN": unquote(token)},
        )
        if response.status_code in {401, 419, 422}:
            raise HirifyAuthError("Hirify login rejected the configured credentials")
        if response.is_error:
            raise HirifyAuthError(f"Hirify login failed (HTTP {response.status_code})")

    async def _contact_response(self, slug: str) -> httpx.Response:
        token = unquote(self._client.cookies.get("XSRF-TOKEN", ""))
        return await self._client.post(
            f"/api/vacancies/{slug}/contacts",
            json={},
            headers={"X-XSRF-TOKEN": token} if token else {},
        )

    async def get_contact(self, job_url: str) -> Contact | None:
        parsed = urlparse(job_url)
        match = _SLUG_RE.match(parsed.path)
        if not is_hirify_job_url(job_url) or not match:
            return None
        response = await self._contact_response(match.group(1))
        if response.status_code in {401, 419}:
            await self.login()
            response = await self._contact_response(match.group(1))
        if response.status_code in {401, 419}:
            raise HirifyAuthError("Hirify session is unauthorized after login")
        if response.is_error:
            raise HirifyError(f"Hirify contacts request failed (HTTP {response.status_code})")
        return parse_contacts_response(response.json())
