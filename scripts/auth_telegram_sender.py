"""One-time interactive authorization for the Telegram sender account."""

import asyncio

import config
from telegram_sender import TelegramSender


async def main() -> None:
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        raise SystemExit("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")
    sender = TelegramSender(config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH, config.TELEGRAM_SESSION_PATH)
    client = sender._client()
    await client.start(phone=config.TELEGRAM_PHONE or None)
    me = await client.get_me()
    print(f"Telegram sender authorized: {me.id}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
