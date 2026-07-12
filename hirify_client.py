"""Hirify contacts adapter backed by Playwright browser storage state."""

from __future__ import annotations

import json
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from browser_auth import BrowserAuthError, login_hirify

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
        return f"https://t.me/{self.value.lstrip('@')}" if self.kind == "telegram" else self.value if self.kind == "url" else ""


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


def load_browser_cookies(state_path: Path) -> tuple[httpx.Cookies, str]:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HirifyAuthError("Hirify browser session is missing") from exc
    cookies = httpx.Cookies()
    xsrf = ""
    for item in payload.get("cookies", []):
        name = str(item.get("name", ""))
        value = str(item.get("value", ""))
        domain = str(item.get("domain", "")).lstrip(".") or "hirify.me"
        path = str(item.get("path", "/"))
        if name and value:
            cookies.set(name, value, domain=domain, path=path)
            if name == "XSRF-TOKEN":
                xsrf = unquote(value)
    if not xsrf:
        raise HirifyAuthError("Hirify browser session has no XSRF token")
    return cookies, xsrf


class HirifyClient:
    def __init__(
        self,
        email: str,
        password: str,
        state_path: Path,
        *,
        browser_executable: str = "/usr/bin/chromium",
        transport: httpx.AsyncBaseTransport | None = None,
        login=login_hirify,
    ):
        self.email = email
        self.password = password
        self.state_path = state_path
        self.browser_executable = browser_executable
        self.transport = transport
        self.login = login
        self._client = None
        self._xsrf = ""
        self._auth_lock = asyncio.Lock()

    async def _open_client(self, refresh: bool = False) -> None:
        async with self._auth_lock:
            if self._client is not None and not refresh:
                return
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            if refresh or not self.state_path.is_file():
                try:
                    await self.login(self.email, self.password, self.state_path, self.browser_executable)
                except BrowserAuthError as exc:
                    raise HirifyAuthError(str(exc)) from exc
            try:
                cookies, self._xsrf = load_browser_cookies(self.state_path)
            except HirifyAuthError:
                if refresh:
                    raise
                try:
                    await self.login(self.email, self.password, self.state_path, self.browser_executable)
                except BrowserAuthError as exc:
                    raise HirifyAuthError(str(exc)) from exc
                cookies, self._xsrf = load_browser_cookies(self.state_path)
            self._client = httpx.AsyncClient(
            base_url=_API_BASE,
            cookies=cookies,
            follow_redirects=True,
            timeout=15.0,
            transport=self.transport,
            headers={"Accept": "application/json", "Origin": "https://hirify.me", "Referer": "https://hirify.me/"},
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        if self._client is not None:
            await self._client.aclose()

    async def get_contact(self, job_url: str) -> Contact | None:
        parsed = urlparse(job_url)
        match = _SLUG_RE.match(parsed.path)
        if not is_hirify_job_url(job_url) or not match:
            return None
        await self._open_client()
        response = await self._client.post(
            f"/api/vacancies/{match.group(1)}/contacts",
            json={},
            headers={"X-XSRF-TOKEN": self._xsrf},
        )
        if response.status_code in {401, 419}:
            await self._open_client(refresh=True)
            response = await self._client.post(
                f"/api/vacancies/{match.group(1)}/contacts",
                json={},
                headers={"X-XSRF-TOKEN": self._xsrf},
            )
        if response.status_code in {401, 419}:
            raise HirifyAuthError("Hirify login succeeded but the contacts session is unauthorized")
        if response.is_error:
            raise HirifyError(f"Hirify contacts request failed (HTTP {response.status_code})")
        return parse_contacts_response(response.json())
