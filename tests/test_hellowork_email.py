import asyncio
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from jobbot.email_store import claim_next_offer, finish_offer, get_offer, record_email_offers
from jobbot.integrations.hellowork_email import (
    HelloWorkEmailError,
    parse_hellowork_alert,
    resolve_alert_offers,
    resolve_offer_url,
)


def alert_bytes(count=10, sender="Hellowork notifications <notification@emails.hellowork.com>"):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "bot@example.com"
    message["Message-ID"] = "<alert-1@hellowork.com>"
    message["List-ID"] = "PushOffreClassique"
    message["Authentication-Results"] = (
        "mx.google.com; dkim=pass header.i=@emails.hellowork.com"
    )
    links = "".join(
        f'<a href="https://emails.hellowork.com/clic/message/{index}/token">Job {index}</a>'
        f'<a href="https://emails.hellowork.com/clic/message/{index}/token">Voir offre</a>'
        for index in range(1, count + 1)
    )
    message.set_content("HelloWork jobs")
    message.add_alternative(f"<html><body>{links}</body></html>", subtype="html")
    return message.as_bytes()


class HelloWorkEmailTests(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_and_deduplicates_all_offers(self):
        alert = parse_hellowork_alert(alert_bytes())

        async def resolver(url):
            identifier = url.split("/")[-2]
            return identifier, f"https://www.hellowork.com/fr-fr/emplois/{identifier}.html"

        offers = await resolve_alert_offers(alert, resolver=resolver)
        self.assertEqual(len(alert.tracking_urls), 10)
        self.assertEqual(len(offers), 10)
        self.assertEqual(offers[0][0], "1")

    async def test_rejects_spoofed_or_unauthenticated_sender(self):
        with self.assertRaises(HelloWorkEmailError):
            parse_hellowork_alert(alert_bytes(sender="attacker@example.com"))
        raw = alert_bytes().replace(b"dkim=pass", b"dkim=fail")
        with self.assertRaises(HelloWorkEmailError):
            parse_hellowork_alert(raw)

    async def test_tracking_redirect_must_finish_at_canonical_offer(self):
        def handler(request):
            if request.url.host == "emails.hellowork.com":
                return httpx.Response(
                    302,
                    headers={"location": "https://www.hellowork.com/fr-fr/emplois/81835625.html"},
                    request=request,
                )
            return httpx.Response(200, text="offer", request=request)

        with patch(
            "jobbot.integrations.hellowork_email.validate_public_url",
            new=AsyncMock(),
        ):
            result = await resolve_offer_url(
                "https://emails.hellowork.com/clic/message/1/token",
                transport=httpx.MockTransport(handler),
            )
        self.assertEqual(
            result,
            ("81835625", "https://www.hellowork.com/fr-fr/emplois/81835625.html"),
        )


class EmailStoreTests(unittest.TestCase):
    def test_receipt_and_offer_queue_are_durable_and_idempotent(self):
        offers = (("1", "https://www.hellowork.com/fr-fr/emplois/1.html"),)
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            first = record_email_offers(
                db, mailbox_key="bot", uid_validity="7", uid="9",
                message_id="m1", raw_message=b"raw", offers=offers,
            )
            duplicate = record_email_offers(
                db, mailbox_key="bot", uid_validity="7", uid="9",
                message_id="m1", raw_message=b"raw", offers=offers,
            )
            self.assertEqual(first, (1, 0, False))
            self.assertEqual(duplicate, (0, 1, True))
            claimed = claim_next_offer(db)
            self.assertEqual(claimed["offer_id"], "1")
            finish_offer(db, "1", "completed")
            self.assertEqual(get_offer(db, "1")["status"], "completed")
            self.assertIsNone(claim_next_offer(db))


if __name__ == "__main__":
    unittest.main()
