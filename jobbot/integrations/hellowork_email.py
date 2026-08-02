"""Validate HelloWork alert mail and extract canonical offer URLs."""

from __future__ import annotations

import imaplib
import base64
import logging
import re
from dataclasses import dataclass, field
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
_MAX_TRACKING_TOKEN_BYTES = 16_384

logger = logging.getLogger(__name__)


class HelloWorkEmailError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "hellowork_email_error",
        permanent: bool = False,
        diagnostics: dict[str, int] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.permanent = permanent
        self.diagnostics = dict(diagnostics or {})


@dataclass(frozen=True)
class HelloWorkAlert:
    message_id: str
    tracking_urls: tuple[str, ...]
    diagnostics: dict[str, int] = field(default_factory=dict, compare=False)


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


def _canonical_offer(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    match = _OFFER_RE.match(parsed.path)
    if (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() in {"hellowork.com", "www.hellowork.com"}
        and match
    ):
        offer_id = match.group("id")
        return offer_id, f"https://www.hellowork.com/fr-fr/emplois/{offer_id}.html"
    return None


def _embedded_offer(tracking_url: str) -> tuple[bool, tuple[str, str] | None]:
    """Return whether a tracking payload decoded, plus its canonical offer if any."""
    parsed = urlparse(tracking_url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != _TRACKING_HOST
        or not parsed.path.startswith("/clic/")
    ):
        return False, None
    token = parsed.path.rsplit("/", 1)[-1]
    if not token or len(token) > _MAX_TRACKING_TOKEN_BYTES:
        return False, None
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False, None
    target = re.search(r"https?://\S+", decoded)
    if target is None:
        return False, None
    return True, _canonical_offer(target.group(0))


def parse_hellowork_alert(raw: bytes) -> HelloWorkAlert:
    if len(raw) > _MAX_MESSAGE_BYTES:
        raise HelloWorkEmailError(
            "message exceeds the 5 MB limit", code="message_too_large", permanent=True,
            diagnostics={"message_bytes": len(raw)},
        )
    message = BytesParser(policy=policy.default).parsebytes(raw)
    candidates = tuple(_iter_messages(message))
    diagnostics = {
        "message_bytes": len(raw),
        "message_candidates": len(candidates),
        "mime_parts": 0,
        "text_parts": 0,
        "html_parts": 0,
        "anchors": 0,
        "http_urls": 0,
        "hellowork_host_urls": 0,
        "tracking_urls": 0,
        "decoded_job_links": 0,
        "decoded_non_job_links": 0,
    }
    links: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        for part in item.walk():
            diagnostics["mime_parts"] += 1
            if part.get_content_type() not in {"text/html", "text/plain"}:
                continue
            diagnostics["text_parts"] += 1
            try:
                content = part.get_content()
            except (LookupError, UnicodeError):
                continue
            if part.get_content_type() == "text/html":
                diagnostics["html_parts"] += 1
                values = [str(tag.get("href") or "") for tag in BeautifulSoup(content, "html.parser").find_all("a")]
                diagnostics["anchors"] += len(values)
            else:
                values = re.findall(r"https?://[^\s<>\"']+", content)
            for value in values:
                parsed = urlparse(value)
                if parsed.scheme in {"http", "https"} and parsed.hostname:
                    diagnostics["http_urls"] += 1
                if (parsed.hostname or "").casefold() == _TRACKING_HOST:
                    diagnostics["hellowork_host_urls"] += 1
                if (
                    parsed.scheme == "https"
                    and (parsed.hostname or "").casefold() == _TRACKING_HOST
                    and parsed.path.startswith("/clic/")
                    and value not in seen
                ):
                    seen.add(value)
                    links.append(value)
    diagnostics["tracking_urls"] = len(links)
    for link in links:
        decoded, offer = _embedded_offer(link)
        if decoded and offer:
            diagnostics["decoded_job_links"] += 1
        elif decoded:
            diagnostics["decoded_non_job_links"] += 1
    if not links:
        raise HelloWorkEmailError(
            "no HelloWork tracking links found", code="no_tracking_links",
            permanent=True, diagnostics=diagnostics,
        )
    return HelloWorkAlert(
        str(message.get("Message-ID", "")), tuple(links), diagnostics
    )


async def resolve_offer_url(
    tracking_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, str] | None:
    """Resolve one tracking URL and return ``(offer_id, canonical_url)``."""
    decoded, embedded = _embedded_offer(tracking_url)
    if decoded:
        return embedded
    current = tracking_url
    headers = {"User-Agent": "getajob/1.0", "Accept": "text/html,*/*"}
    async with httpx.AsyncClient(timeout=20, headers=headers, transport=transport) as client:
        for _ in range(6):
            parsed = urlparse(current)
            host = (parsed.hostname or "").casefold()
            if parsed.scheme != "https" or host not in {_TRACKING_HOST, "hellowork.com", "www.hellowork.com"}:
                raise HelloWorkEmailError(
                    "tracking link redirected outside HelloWork",
                    code="redirect_outside_hellowork",
                )
            await validate_public_url(current)
            response = await client.get(current, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise HelloWorkEmailError(
                        "tracking redirect has no destination", code="redirect_missing_location"
                    )
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            return _canonical_offer(current)
    raise HelloWorkEmailError("too many HelloWork redirects", code="too_many_redirects")


async def resolve_alert_offers(
    alert: HelloWorkAlert,
    *,
    resolver: Callable[[str], object] | None = None,
) -> tuple[tuple[str, str], ...]:
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    failed = 0
    for tracking_url in alert.tracking_urls:
        try:
            resolved = await (resolver(tracking_url) if resolver else resolve_offer_url(tracking_url))
        except Exception as exc:
            failed += 1
            logger.warning(
                "HelloWork tracking candidate skipped error_type=%s",
                type(exc).__name__,
            )
            continue
        if resolved and resolved[0] not in seen:
            seen.add(resolved[0])
            results.append(resolved)
    logger.info(
        "HelloWork offer resolution tracking=%s offers=%s skipped=%s failed=%s",
        len(alert.tracking_urls), len(results),
        len(alert.tracking_urls) - len(results) - failed, failed,
    )
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

    def fetch_uids(self, uids: tuple[str, ...]) -> tuple[str, tuple[InboxMessage, ...], tuple[str, ...]]:
        """Fetch specific messages, including seen mail, and report missing UIDs."""
        if not uids:
            return "unknown", (), ()
        with imaplib.IMAP4_SSL(self.host, self.port) as client:
            client.login(self.username, self.password)
            status, data = client.status(self.mailbox, "(UIDVALIDITY)")
            if status != "OK":
                raise HelloWorkEmailError(
                    "could not read inbox UIDVALIDITY", code="imap_uidvalidity"
                )
            match = re.search(rb"UIDVALIDITY\s+(\d+)", data[0] or b"")
            uid_validity = match.group(1).decode() if match else "unknown"
            if client.select(self.mailbox, readonly=True)[0] != "OK":
                raise HelloWorkEmailError("could not select inbox", code="imap_select")
            messages: list[InboxMessage] = []
            missing: list[str] = []
            for uid in uids:
                status, payload = client.uid("fetch", uid, "(BODY.PEEK[])")
                raw = next((item[1] for item in payload if isinstance(item, tuple)), b"")
                if status != "OK" or not raw:
                    missing.append(uid)
                else:
                    messages.append(InboxMessage(uid, raw))
            return uid_validity, tuple(messages), tuple(missing)

    def mark_seen(self, uid: str) -> None:
        with imaplib.IMAP4_SSL(self.host, self.port) as client:
            client.login(self.username, self.password)
            if client.select(self.mailbox, readonly=False)[0] != "OK":
                raise HelloWorkEmailError("could not select inbox")
            if client.uid("store", uid, "+FLAGS", "(\\Seen)")[0] != "OK":
                raise HelloWorkEmailError("could not mark inbox message handled")

