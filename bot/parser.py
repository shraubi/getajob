"""Source-neutral vacancy parsing with conservative network safety checks."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from enum import StrEnum
from html import unescape
from html.parser import HTMLParser
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class JobSource(StrEnum):
    HELLOWORK = "hellowork"
    TELEGRAM = "telegram"
    GENERIC_WEB = "generic_web"


@dataclass(frozen=True)
class Vacancy:
    title: str
    company: str
    description: str
    source_url: str
    application_url: str
    source: JobSource


class VacancyParser(Protocol):
    async def parse(self, url: str) -> Vacancy: ...


def classify_source(url: str) -> JobSource:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if host == "hellowork.com" or host.endswith(".hellowork.com"):
        return JobSource.HELLOWORK
    if host in {"t.me", "telegram.me", "telegram.org"} or host.endswith(".telegram.org"):
        return JobSource.TELEGRAM
    return JobSource.GENERIC_WEB


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("Only public HTTP(S) URLs are supported")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Non-standard URL ports are not supported")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise ValueError("Job host could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Private or local network destinations are forbidden")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(url: str, max_redirects: int = 3, timeout: float = 8.0) -> tuple[str, str]:
    opener = build_opener(_NoRedirect)
    current = url
    for _ in range(max_redirects + 1):
        _validate_public_url(current)
        request = Request(current, headers={"User-Agent": "getajob/0.1 (+vacancy parser)"})
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise ValueError(f"Job page returned HTTP {exc.code}") from exc
            location = exc.headers.get("Location")
            if not location:
                raise ValueError("Redirect had no destination") from exc
            current = urljoin(current, location)
            continue
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("Job URL did not return HTML")
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError("Job page is larger than 2 MB")
        charset = response.headers.get_content_charset() or "utf-8"
        return body.decode(charset, errors="replace"), response.geturl()
    raise ValueError("Too many redirects")


class _JobPostingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts: list[str] = []
        self._in_json_ld = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self._in_json_ld = True
            self._buffer = []

    def handle_data(self, data):
        if self._in_json_ld:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in_json_ld:
            self.scripts.append("".join(self._buffer))
            self._in_json_ld = False


def _job_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _job_nodes(item)
    elif isinstance(value, dict):
        if value.get("@type") == "JobPosting" or "JobPosting" in value.get("@type", []):
            yield value
        yield from _job_nodes(value.get("@graph", []))


def _plain_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(value)).strip()


def parse_html(html: str, source_url: str, final_url: str | None = None) -> Vacancy:
    parser = _JobPostingParser()
    parser.feed(html)
    for raw in parser.scripts:
        try:
            candidates = _job_nodes(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
        for job in candidates:
            organization = job.get("hiringOrganization") or {}
            company = organization.get("name", "") if isinstance(organization, dict) else str(organization)
            title = _plain_text(str(job.get("title", "")))
            description = _plain_text(str(job.get("description", "")))
            if title and description:
                apply_url = job.get("url") or final_url or source_url
                return Vacancy(title, _plain_text(company) or "Unknown company", description,
                               source_url, urljoin(final_url or source_url, apply_url),
                               classify_source(source_url))
    raise ValueError("No schema.org JobPosting data found on the page")


async def parse_vacancy(url: str) -> Vacancy:
    html, final_url = await asyncio.to_thread(_fetch, url)
    return parse_html(html, url, final_url)


async def extract_jd(text: str) -> str:
    """Compatibility adapter for the earlier handler API."""
    match = re.search(r"https?://[^\s<>]+", text)
    if not match:
        return text.strip()
    vacancy = await parse_vacancy(match.group(0).rstrip(".,);]"))
    return f"{vacancy.title}\n{vacancy.company}\n\n{vacancy.description}"
