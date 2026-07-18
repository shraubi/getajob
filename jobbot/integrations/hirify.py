"""Authenticated Hirify contacts adapter using Laravel Sanctum SPA auth."""

from __future__ import annotations

import asyncio
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
    company_title: str = ""

    @property
    def target_url(self) -> str:
        if self.kind == "telegram":
            return f"https://t.me/{self.value.lstrip('@')}"
        return self.value if self.kind == "url" else ""


@dataclass(frozen=True)
class DirectApplication:
    vacancy_id: int


def is_hirify_job_url(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.hostname or "").casefold() in {"hirify.me", "www.hirify.me"} and bool(_SLUG_RE.match(parsed.path))


def parse_contacts_response(payload: dict) -> Contact | None:
    company_title = str(payload.get("company_title", "")).strip()
    for item in payload.get("contacts", []):
        kind = str(item.get("type", "")).casefold()
        value = str(item.get("value", "")).strip()
        short_code = str(item.get("short_code", "")).strip()
        if kind == "telegram" and re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", value):
            return Contact(kind, value.lstrip("@"), short_code, company_title)
        if kind == "url" and value.startswith(("http://", "https://")):
            return Contact(kind, value, short_code, company_title)
        if kind in {"email", "phone"} and value:
            return Contact(kind, value, short_code, company_title)
    return None


def _completed_profile_id(user: dict) -> int | None:
    profiles = [item for item in user.get("user_profiles", []) if item.get("status") == "completed"]
    if not profiles:
        return None
    primary_id = user.get("profile_id")
    selected = next((item for item in profiles if item.get("profile_id") == primary_id), profiles[0])
    try:
        return int(selected["profile_id"])
    except (KeyError, TypeError, ValueError):
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
        self._authenticated = False
        self._login_lock = asyncio.Lock()
        self._contact_locks: dict[str, asyncio.Lock] = {}
        self._contact_cache: dict[str, Contact | None] = {}
        self._direct_cache: dict[str, DirectApplication | None] = {}
        self._profile_id: int | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self._client.aclose()

    async def login(self, *, force: bool = False) -> None:
        async with self._login_lock:
            if self._authenticated and not force:
                return
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
            if "json" in response.headers.get("content-type", ""):
                self._profile_id = _completed_profile_id(response.json()) or self._profile_id
            self._authenticated = True

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
        slug = match.group(1)
        if slug in self._contact_cache:
            return self._contact_cache[slug]
        lock = self._contact_locks.setdefault(slug, asyncio.Lock())
        async with lock:
            if slug in self._contact_cache:
                return self._contact_cache[slug]
            await self.login()
            response = await self._contact_response(slug)
            if response.status_code in {401, 419}:
                self._authenticated = False
                await self.login(force=True)
                response = await self._contact_response(slug)
            if response.status_code in {401, 419}:
                raise HirifyAuthError("Hirify session is unauthorized after login")
            if response.is_error:
                raise HirifyError(f"Hirify contacts request failed (HTTP {response.status_code})")
            contact = parse_contacts_response(response.json())
            self._contact_cache[slug] = contact
            return contact

    async def get_direct_application(self, job_url: str) -> DirectApplication | None:
        parsed = urlparse(job_url)
        match = _SLUG_RE.match(parsed.path)
        if not is_hirify_job_url(job_url) or not match:
            return None
        slug = match.group(1)
        if slug in self._direct_cache:
            return self._direct_cache[slug]
        response = await self._client.get(f"/api/vacancies/{slug}")
        if response.is_error:
            raise HirifyError(f"Hirify vacancy request failed (HTTP {response.status_code})")
        payload = response.json()
        result = DirectApplication(int(payload["id"])) if payload.get("can_apply_directly") else None
        self._direct_cache[slug] = result
        return result

    async def _authenticated_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        await self.login()
        token = unquote(self._client.cookies.get("XSRF-TOKEN", ""))
        headers = dict(kwargs.pop("headers", {}))
        if method.upper() != "GET" and token:
            headers["X-XSRF-TOKEN"] = token
        response = await self._client.request(method, path, headers=headers, **kwargs)
        if response.status_code in {401, 419}:
            self._authenticated = False
            await self.login(force=True)
            token = unquote(self._client.cookies.get("XSRF-TOKEN", ""))
            if method.upper() != "GET" and token:
                headers["X-XSRF-TOKEN"] = token
            response = await self._client.request(method, path, headers=headers, **kwargs)
        return response

    async def apply_direct(self, vacancy_id: int) -> int:
        existing = await self._authenticated_request("GET", f"/api/user/applications-for-vacancy/{vacancy_id}")
        if existing.is_error:
            raise HirifyError(f"Hirify application check failed (HTTP {existing.status_code})")
        applications = existing.json() or []
        if applications:
            raise HirifyError("This Hirify vacancy already has an application")

        if self._profile_id is None:
            user_response = await self._authenticated_request("GET", "/auth/user")
            if user_response.is_error:
                raise HirifyError(f"Hirify profile request failed (HTTP {user_response.status_code})")
            self._profile_id = _completed_profile_id(user_response.json())
        if self._profile_id is None:
            raise HirifyError("Hirify has no completed profile to apply with")

        response = await self._authenticated_request(
            "POST",
            "/api/user/applications",
            json={
                "vacancy_id": vacancy_id,
                "profile_id": self._profile_id,
                "cover_letter": None,
                "source": "direct",
            },
        )
        if response.is_error:
            detail = response.json().get("message", "") if "json" in response.headers.get("content-type", "") else ""
            raise HirifyError(detail or f"Hirify direct application failed (HTTP {response.status_code})")
        payload = response.json()
        application = payload.get("data", payload) if isinstance(payload, dict) else {}
        return int(application.get("id", 0))
