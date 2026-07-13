import tempfile
import unittest
from pathlib import Path

from telegram_sender import TelegramPeerFloodError, TelegramSender, TelegramSenderError, telegram_username


class FakeSent:
    id = 42


class FakeClient:
    def __init__(self, authorized=True):
        self.authorized = authorized
        self.call = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def is_user_authorized(self):
        return self.authorized

    async def send_file(self, username, path, caption):
        self.call = (username, path, caption)
        return FakeSent()


class PeerFloodError(Exception):
    pass


class TelegramSenderTests(unittest.IsolatedAsyncioTestCase):
    def test_accepts_raw_hirify_username(self):
        self.assertEqual(telegram_username("brandiumsu"), "brandiumsu")
        self.assertEqual(telegram_username("@brandiumsu"), "brandiumsu")

    def test_rejects_invalid_username(self):
        with self.assertRaises(TelegramSenderError):
            telegram_username("https://example.com")

    async def test_sends_resume_as_captioned_file(self):
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"pdf")
            fake = FakeClient()
            sender = TelegramSender(1, "hash", Path(directory) / "session")
            sender._client = lambda: fake
            message_id = await sender.send_resume("brandiumsu", "Hello", resume)
            self.assertEqual(message_id, 42)
            self.assertEqual(fake.call, ("brandiumsu", str(resume), "Hello"))

    async def test_requires_authorized_session(self):
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"pdf")
            sender = TelegramSender(1, "hash", Path(directory) / "session")
            sender._client = lambda: FakeClient(authorized=False)
            with self.assertRaisesRegex(TelegramSenderError, "not authorized"):
                await sender.send_resume("brandiumsu", "Hello", resume)

    async def test_converts_peer_flood_to_safe_domain_error(self):
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"pdf")
            fake = FakeClient()
            async def fail(*_args, **_kwargs):
                raise PeerFloodError("too many requests")
            fake.send_file = fail
            sender = TelegramSender(1, "hash", Path(directory) / "session")
            sender._client = lambda: fake
            with self.assertRaisesRegex(TelegramPeerFloodError, "automatic retries are paused"):
                await sender.send_resume("brandiumsu", "Hello", resume)


if __name__ == "__main__":
    unittest.main()

