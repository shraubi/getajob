"""Validate HelloWork alert mail and extract canonical offer URLs."""

from __future__ import annotations

import imaplib
import re
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from jobbot.integrations.job_page import validate_public_url


_TRACKING_HOST = "emails.hellowork.com"
_OFFER_RE = re.compile(r"^/fr-fr/emplois/(?P<id>[0-9]+)\.html/?$")
_MAX_MESSAGE_BYTES = 5_000_000


class HelloWorkEmailError(RuntimeError):
    pass


@dataclass(frozen=True)
class HelloWorkAlert:
    message_id: str
    tracking_urls: tuple[str, ...]


@dataclass(frozen=True)
class InboxMessage:
    uid: str
    raw: bytes


def _iter_messages(message: Message):
    yield message
    for part in message.walk():
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list):
                for nested in payload:
                    yield from _iter_messages(nested)


def parse_hellowork_alert(raw: bytes) -> HelloWorkAlert:
    if len(raw) > _MAX_MESSAGE_BYTES:
        raise HelloWorkEmailError("message exceeds the 5 MB limit")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    candidates = tuple(_iter_messages(message))

    links: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        for part in item.walk():
            if part.get_content_type() not in {"text/html", "text/plain"}:
                continue
            try:
                content = part.get_content()
            except (LookupError, UnicodeError):
                continue
            if part.get_content_type() == "text/html":
                values = [str(tag.get("href") or "") for tag in BeautifulSoup(content, "html.parser").find_all("a")]
            else:
                values = re.findall(r"https://emails\.hellowork\.com/clic/[^\s<>\"']+", content)
            for value in values:
                parsed = urlparse(value)
                if (
                    parsed.scheme == "https"
                    and (parsed.hostname or "").casefold() == _TRACKING_HOST
                    and parsed.path.startswith("/clic/")
                    and value not in seen
                ):
                    seen.add(value)
                    links.append(value)
    if not links:
        raise HelloWorkEmailError("no HelloWork tracking links found")
    return HelloWorkAlert(str(message.get("Message-ID", "")), tuple(links))


async def resolve_offer_url(
    tracking_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, str] | None:
    """Resolve one tracking URL and return ``(offer_id, canonical_url)``."""
    current = tracking_url
    headers = {"User-Agent": "getajob/1.0", "Accept": "text/html,*/*"}
    async with httpx.AsyncClient(timeout=20, headers=headers, transport=transport) as client:
        for _ in range(6):
            parsed = urlparse(current)
            host = (parsed.hostname or "").casefold()
            if parsed.scheme != "https" or host not in {_TRACKING_HOST, "hellowork.com", "www.hellowork.com"}:
                raise HelloWorkEmailError("tracking link redirected outside HelloWork")
            await validate_public_url(current)
            response = await client.get(current, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise HelloWorkEmailError("tracking redirect has no destination")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            match = _OFFER_RE.match(parsed.path)
            if host in {"hellowork.com", "www.hellowork.com"} and match:
                offer_id = match.group("id")
                return offer_id, f"https://www.hellowork.com/fr-fr/emplois/{offer_id}.html"
            return None
    raise HelloWorkEmailError("too many HelloWork redirects")


async def resolve_alert_offers(
    alert: HelloWorkAlert,
    *,
    resolver: Callable[[str], object] | None = None,
) -> tuple[tuple[str, str], ...]:
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tracking_url in alert.tracking_urls:
        resolved = await (resolver(tracking_url) if resolver else resolve_offer_url(tracking_url))
        if resolved and resolved[0] not in seen:
            seen.add(resolved[0])
            results.append(resolved)
    return tuple(results)


class GmailInbox:
    """Small synchronous IMAP adapter; callers run it in ``asyncio.to_thread``."""

    def __init__(self, host: str, port: int, username: str, password: str, mailbox: str = "INBOX"):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.mailbox = mailbox

    def unread(self) -> tuple[str, tuple[InboxMessage, ...]]:
        with imaplib.IMAP4_SSL(self.host, self.port) as client:
            client.login(self.username, self.password)
            status, data = client.status(self.mailbox, "(UIDVALIDITY)")
            if status != "OK":
                raise HelloWorkEmailError("could not read inbox UIDVALIDITY")
            match = re.search(rb"UIDVALIDITY\s+(\d+)", data[0] or b"")
            uid_validity = match.group(1).decode() if match else "unknown"
            if client.select(self.mailbox, readonly=False)[0] != "OK":
                raise HelloWorkEmailError("could not select inbox")
            status, ids = client.uid("search", None, "UNSEEN")
            if status != "OK":
                raise HelloWorkEmailError("could not search inbox")
            messages: list[InboxMessage] = []
            for uid_bytes in (ids[0] or b"").split():
                uid = uid_bytes.decode("ascii", errors="strict")
                status, payload = client.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK":
                    raise HelloWorkEmailError("could not fetch inbox message")
                raw = next((item[1] for item in payload if isinstance(item, tuple)), b"")
                messages.append(InboxMessage(uid, raw))
            return uid_validity, tuple(messages)

    def mark_seen(self, uid: str) -> None:
        with imaplib.IMAP4_SSL(self.host, self.port) as client:
            client.login(self.username, self.password)
            if client.select(self.mailbox, readonly=False)[0] != "OK":
                raise HelloWorkEmailError("could not select inbox")
            if client.uid("store", uid, "+FLAGS", "(\\Seen)")[0] != "OK":
                raise HelloWorkEmailError("could not mark inbox message handled")
