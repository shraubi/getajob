"""Read-only Telegram history retrieval for Ralph."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import httpx

from .models import ChatMessage

_MARKER_RE = re.compile(r"(?m)^Ralph-Run:\s*([0-9a-f]{32})\s*$")
_MARKER_SEARCH_LIMIT = 100
_MESSAGE_LIMIT = 30


class RalphHistoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Marker:
    message: ChatMessage
    run_id: str


@dataclass(frozen=True)
class HistoryResult:
    peer_key: str
    marker: Marker | None
    boundary_message_id: int
    messages: tuple[ChatMessage, ...]
    seed_request: ChatMessage | None
    has_more: bool


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RalphHistoryError("--since must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def marker_run_id(message: ChatMessage) -> str | None:
    if not message.outgoing:
        return None
    match = _MARKER_RE.search(message.text)
    return match.group(1) if match else None


def select_latest_marker(messages: tuple[ChatMessage, ...]) -> Marker | None:
    candidates = [
        Marker(message, run_id)
        for message in messages
        if (run_id := marker_run_id(message)) is not None
    ]
    return max(candidates, key=lambda item: item.message.id, default=None)


def _buttons(raw) -> tuple[str, ...]:
    rows = getattr(raw, "buttons", None) or ()
    return tuple(
        str(getattr(button, "text", "")).strip()
        for row in rows
        for button in (row or ())
        if str(getattr(button, "text", "")).strip()
    )


def from_telethon_message(raw) -> ChatMessage:
    reply_to = getattr(raw, "reply_to_msg_id", None)
    if reply_to is None:
        reply = getattr(raw, "reply_to", None)
        reply_to = getattr(reply, "reply_to_msg_id", None) if reply else None
    return ChatMessage(
        id=int(raw.id),
        date=raw.date.astimezone(timezone.utc),
        outgoing=bool(getattr(raw, "out", False)),
        text=str(getattr(raw, "raw_text", "") or ""),
        has_document=getattr(raw, "document", None) is not None,
        buttons=_buttons(raw),
        edit_date=(raw.edit_date.astimezone(timezone.utc) if getattr(raw, "edit_date", None) else None),
        reply_to_message_id=(int(reply_to) if reply_to is not None else None),
    )


async def resolve_bot_username(bot_token: str) -> str:
    if not bot_token:
        raise RalphHistoryError("TELEGRAM_BOT_TOKEN is required")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"https://api.telegram.org/bot{bot_token}/getMe")
            response.raise_for_status()
            payload = response.json()
        username = str(payload.get("result", {}).get("username", "")).strip()
    except Exception as exc:
        raise RalphHistoryError("Could not resolve the configured Telegram bot") from exc
    if not username:
        raise RalphHistoryError("Configured Telegram token did not resolve to a bot username")
    return username


async def find_latest_marker(client, entity) -> Marker | None:
    found: list[ChatMessage] = []
    async for raw in client.iter_messages(entity, search="Ralph-Run:", limit=_MARKER_SEARCH_LIMIT):
        message = from_telethon_message(raw)
        if marker_run_id(message):
            found.append(message)
    return select_latest_marker(tuple(found))


async def _collect(
    iterator: AsyncIterator[object], *, keep_latest: bool = False
) -> tuple[tuple[ChatMessage, ...], bool]:
    messages = [from_telethon_message(raw) async for raw in iterator]
    messages.sort(key=lambda item: item.id)
    has_more = len(messages) > _MESSAGE_LIMIT
    if has_more:
        messages = messages[-_MESSAGE_LIMIT:] if keep_latest else messages[:_MESSAGE_LIMIT]
    return tuple(messages), has_more


async def read_history(
    client,
    entity,
    *,
    peer_key: str,
    checkpoint_message_id: int | None,
    since: datetime | None,
    replay_latest_run: bool,
) -> HistoryResult:
    marker = await find_latest_marker(client, entity)
    marker_id = marker.message.id if marker else 0
    seed = None

    # An explicit timestamp is an override. Normal runs require no timestamp:
    # they continue from the checkpoint, then the latest marker, then the
    # most recent 30 messages when neither boundary exists.
    if since is not None:
        boundary_id = 0
        iterator = client.iter_messages(
            entity, offset_date=since, reverse=True, limit=_MESSAGE_LIMIT + 1
        )
        messages, has_more = await _collect(iterator)
    else:
        boundary_id = marker_id
        if checkpoint_message_id is not None and not replay_latest_run:
            boundary_id = max(boundary_id, checkpoint_message_id)
        if boundary_id:
            iterator = client.iter_messages(
                entity, min_id=boundary_id, reverse=True, limit=_MESSAGE_LIMIT + 1
            )
            messages, has_more = await _collect(iterator)
            seed = marker.message if marker and boundary_id == marker_id else None
        else:
            iterator = client.iter_messages(entity, limit=_MESSAGE_LIMIT + 1)
            messages, _ = await _collect(iterator, keep_latest=True)
            has_more = False

    return HistoryResult(
        peer_key, marker, boundary_id, messages, seed, has_more
    )


async def fetch_telegram_history(
    *,
    api_id: int,
    api_hash: str,
    session_path: Path,
    bot_username: str,
    checkpoint_message_id: int | None,
    since: datetime | None,
    replay_latest_run: bool,
) -> HistoryResult:
    if not api_id or not api_hash:
        raise RalphHistoryError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise RalphHistoryError("Telethon is not installed") from exc

    client = TelegramClient(
        str(session_path), api_id, api_hash, receive_updates=False
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RalphHistoryError("Telegram user session is not authorized")
        try:
            entity = await client.get_entity(bot_username)
        except Exception as exc:
            raise RalphHistoryError("Configured bot conversation was not found") from exc
        return await read_history(
            client,
            entity,
            peer_key=bot_username.casefold(),
            checkpoint_message_id=checkpoint_message_id,
            since=since,
            replay_latest_run=replay_latest_run,
        )
    finally:
        await client.disconnect()
