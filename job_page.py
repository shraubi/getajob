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
    r"\u0441\u0432\u044f\u0437\u0430\u0442\u044c\u0441\u044f|\u043d\u0430\u043f\u0438\u0441\u0430\u0442\u044c)\b",
    re.I,
)
_MAX_BYTES = 2_000_000
_MAX_REDIRECTS = 4


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
                        raise JobPageError("Redirect response did not include a destination")
                    current = urljoin(current, location)
                    continue
                if response.status_code in {404, 410}:
                    raise PageUnavailableError(f"Job page is no longer available (HTTP {response.status_code})")
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "html" not in content_type.casefold():
                    raise JobPageError(f"Expected an HTML job page, got {content_type or 'unknown content type'}")
                declared_size = response.headers.get("content-length")
                if declared_size and int(declared_size) > _MAX_BYTES:
                    raise JobPageError("Job page is too large to parse safely")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_BYTES:
                        raise JobPageError("Job page exceeded the safe download limit")
                encoding = response.encoding or "utf-8"
                return bytes(body).decode(encoding, errors="replace"), str(response.url)
    raise JobPageError(f"Job page exceeded {_MAX_REDIRECTS} redirects")


def _job_posting(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = payload if isinstance(payload, list) else payload.get("@graph", [payload]) if isinstance(payload, dict) else []
        for item in candidates:
            item_type = item.get("@type", []) if isinstance(item, dict) else []
            types = {item_type} if isinstance(item_type, str) else set(item_type)
            if "JobPosting" in types:
                return item
    return {}


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def _plain_text(value) -> str:
    if not value:
        return ""
    return BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)


def _application_target(soup: BeautifulSoup, base_url: str) -> tuple[str, bool]:
    for form in soup.find_all("form"):
        searchable = " ".join((form.get("action", ""), form.get_text(" ", strip=True)))
        fields = " ".join(str(tag.get("name", "")) + " " + str(tag.get("placeholder", "")) for tag in form.find_all(["input", "textarea"]))
        if _ACTION_RE.search(searchable) or re.search(r"\b(cv|resume|cover.?letter)\b", fields, re.I):
            return urljoin(base_url, form.get("action") or base_url), True
    ranked: list[tuple[int, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"])
        label = " ".join((anchor.get_text(" ", strip=True), anchor.get("aria-label", ""), anchor.get("title", ""), anchor["href"]))
        if _ACTION_RE.search(label):
            score = 2 if _ACTION_RE.search(anchor.get_text(" ", strip=True)) else 1
            ranked.append((score, href))
    return (max(ranked)[1], False) if ranked else ("", False)


def parse_job_html(html: str, page_url: str) -> ParsedJobPage:
    soup = BeautifulSoup(html, "html.parser")
    posting = _job_posting(soup)
    title = _plain_text(posting.get("title")) or _meta(soup, "og:title", "twitter:title")
    if not title:
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if heading else ""
    organization = posting.get("hiringOrganization", {}) if posting else {}
    company = _plain_text(organization.get("name")) if isinstance(organization, dict) else ""
    company = company or _meta(soup, "og:site_name") or "Unknown company"
    description = _plain_text(posting.get("description")) or _meta(soup, "description", "og:description")
    if len(description) < 80:
        container = soup.find("main") or soup.find("article") or soup.body
        description = container.get_text("\n", strip=True) if container else description
    if not title or len(description) < 40:
        raise JobPageError("The page does not expose enough job information")
    apply_url, has_form = _application_target(soup, page_url)
    category = "application_form" if has_form else "structured_job_page" if posting else "job_page_with_apply_link" if apply_url else "job_page"
    vacancy = Vacancy(title=title[:160], company=company[:120], description=description[:20_000], url=page_url)
    return ParsedJobPage(vacancy=vacancy, source_category=category, apply_url=apply_url, fetched_url=page_url)


async def fetch_job_from_message(message: str) -> ParsedJobPage:
    url = extract_first_url(message)
    html, final_url = await fetch_html(url)
    return parse_job_html(html, final_url)
