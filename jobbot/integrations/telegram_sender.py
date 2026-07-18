"""Telegram user-account sender with an on-disk Telethon session."""

from __future__ import annotations

import re
from pathlib import Path


class TelegramSenderError(RuntimeError):
    pass


class TelegramPeerFloodError(TelegramSenderError):
    """Telegram has temporarily restricted unsolicited outbound messages."""

    pass


def telegram_username(value: str) -> str:
    clean = value.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", clean):
        raise TelegramSenderError("Invalid Telegram username")
    return clean


class TelegramSender:
    def __init__(self, api_id: int, api_hash: str, session_path: Path):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path

    def _client(self):
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise TelegramSenderError("Telethon is not installed") from exc
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        return TelegramClient(str(self.session_path), self.api_id, self.api_hash)

    async def send_resume(self, username: str, message: str, resume_path: Path) -> int:
        if not resume_path.is_file():
            raise TelegramSenderError(f"Resume is missing: {resume_path.name}")
        client = self._client()
        async with client:
            if not await client.is_user_authorized():
                raise TelegramSenderError("Telegram sender account is not authorized")
            try:
                sent = await client.send_file(
                    telegram_username(username),
                    str(resume_path),
                    caption=message,
                )
            except Exception as exc:
                if type(exc).__name__ == "PeerFloodError":
                    raise TelegramPeerFloodError(
                        "Telegram restricted new outbound conversations; automatic retries are paused"
                    ) from exc
                raise
        return int(sent.id)
