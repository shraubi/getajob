"""Send one nonce-based probe to the running bot using the existing user session."""
from __future__ import annotations

import asyncio
import secrets
import sqlite3
import tempfile
from pathlib import Path

from telethon import TelegramClient
from telegram import Bot

from jobbot import config


def _session_file(path: Path) -> Path:
    return path if path.suffix == ".session" else Path(f"{path}.session")


def _copy_session(source: Path, target: Path) -> None:
    """Use SQLite backup so a running sender can keep its own session open."""
    with sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True) as original:
        with sqlite3.connect(target) as snapshot:
            original.backup(snapshot)


async def probe(timeout_seconds: int = 30) -> dict[str, str]:
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        raise RuntimeError("Telegram user API credentials are unavailable")
    source = _session_file(config.TELEGRAM_SESSION_PATH)
    if not source.is_file():
        raise RuntimeError("Telegram user session is unavailable")
    nonce = secrets.token_hex(16)
    async with Bot(config.TELEGRAM_BOT_TOKEN) as bot:
        identity = await bot.get_me()
    if not identity.username:
        raise RuntimeError("Bot has no Telegram username")

    with tempfile.TemporaryDirectory(prefix="jobbot-acceptance-") as directory:
        session_base = Path(directory) / "probe"
        _copy_session(source, _session_file(session_base))
        client = TelegramClient(
            str(session_base), config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH
        )
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram user session is no longer authorized")
            user = await client.get_me()
            if user is None or user.id not in config.ALLOWED_CHAT_IDS:
                raise RuntimeError("Telegram user session is not in the bot allowlist")
            async with client.conversation(identity.username, timeout=timeout_seconds) as chat:
                await chat.send_message(f"/health {nonce}")
                expected = f"jobbot-ready:{nonce}"
                async def receive() -> None:
                    for _ in range(8):
                        response = await chat.get_response()
                        if response.raw_text.strip() == expected:
                            return
                    raise RuntimeError("Bot replied, but not with the probe nonce")
                await asyncio.wait_for(receive(), timeout=timeout_seconds)
        finally:
            await client.disconnect()
    return {"status": "PASS", "check": "telegram_inbound_reply"}


def main() -> int:
    try:
        result = asyncio.run(probe())
    except Exception as exc:
        # Do not print bot tokens, session contents, or Telegram message text.
        print(f"telegram_inbound_reply=FAIL reason={type(exc).__name__}")
        return 1
    print(f"{result['check']}={result['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
