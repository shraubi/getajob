import asyncio
import base64
import os
import sqlite3
import tempfile
import unittest
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("YOUR_CHAT_ID", "1")

from jobbot import email_ingest
from jobbot.email_store import (
    claim_next_offer,
    close_stale_rejected_emails,
    finish_offer,
    get_offer,
    record_email_offers,
    record_rejected_email,
    replayable_rejected_uids,
)
from jobbot.integrations.hellowork_email import (
    InboxMessage,
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


def tracking_url(index: int, target: str) -> str:
    payload = base64.urlsafe_b64encode(
        f"bot@example.invalid|{target}".encode()
    ).decode().rstrip("=")
    return f"https://emails.hellowork.com/clic/campaign/{index}/signature/{payload}"


def production_alert_bytes() -> bytes:
    message = EmailMessage()
    message["From"] = "Hellowork notifications <notification@emails.hellowork.com>"
    message["To"] = "bot@example.invalid"
    message["Message-ID"] = "<production-shape@hellowork.com>"
    message["List-ID"] = "PushOffreInterim"
    message["X-Rj-Cmp"] = "PushOffreInterim"
    message["Authentication-Results"] = (
        "mx.google.com; dkim=pass header.i=@emails.hellowork.com"
    )
    first = "https://www.hellowork.com/fr-fr/emplois/81835625.html?utm_source=email"
    second = "https://www.hellowork.com/fr-fr/emplois/81835626.html"
    links = [
        tracking_url(1, first), tracking_url(2, first),
        tracking_url(3, second), tracking_url(4, second),
        tracking_url(5, "https://www.hellowork.com/fr-fr/page/contact.html"),
        tracking_url(6, "https://www.instagram.com/hellowork_/"),
    ]
    message.set_content(
        "<html><body>" + "".join(f'<a href="{url}">open</a>' for url in links)
        + "</body></html>",
        subtype="html",
    )
    return message.as_bytes()


class HelloWorkEmailTests(unittest.IsolatedAsyncioTestCase):
    async def test_decodes_production_links_without_network_and_skips_non_jobs(self):
        alert = parse_hellowork_alert(production_alert_bytes())
        offers = await resolve_alert_offers(alert)

        self.assertEqual(len(alert.tracking_urls), 6)
        self.assertEqual(alert.diagnostics["decoded_job_links"], 4)
        self.assertEqual(alert.diagnostics["decoded_non_job_links"], 2)
        self.assertEqual(
            offers,
            (
                ("81835625", "https://www.hellowork.com/fr-fr/emplois/81835625.html"),
                ("81835626", "https://www.hellowork.com/fr-fr/emplois/81835626.html"),
            ),
        )

    async def test_encoded_destination_requires_exact_canonical_offer(self):
        self.assertIsNone(await resolve_offer_url(tracking_url(1, "https://example.com/fr-fr/emplois/1.html")))
        self.assertIsNone(await resolve_offer_url(tracking_url(2, "http://www.hellowork.com/fr-fr/emplois/1.html")))
        self.assertIsNone(await resolve_offer_url(tracking_url(3, "https://www.hellowork.com/fr-fr/emplois/not-a-number.html")))

    async def test_one_legacy_resolution_failure_does_not_discard_other_offers(self):
        alert = parse_hellowork_alert(alert_bytes(count=2))

        async def resolver(url):
            if "/1/" in url:
                raise HelloWorkEmailError("broken", code="legacy_broken")
            return "2", "https://www.hellowork.com/fr-fr/emplois/2.html"

        self.assertEqual(
            await resolve_alert_offers(alert, resolver=resolver),
            (("2", "https://www.hellowork.com/fr-fr/emplois/2.html"),),
        )

    async def test_zero_link_error_contains_only_structural_diagnostics(self):
        raw = alert_bytes(count=0)
        with self.assertRaises(HelloWorkEmailError) as raised:
            parse_hellowork_alert(raw)
        self.assertEqual(raised.exception.code, "no_tracking_links")
        self.assertTrue(raised.exception.permanent)
        self.assertEqual(raised.exception.diagnostics["tracking_urls"], 0)
        self.assertIn("anchors", raised.exception.diagnostics)

    async def test_extracts_and_deduplicates_all_offers(self):
        alert = parse_hellowork_alert(alert_bytes())

        async def resolver(url):
            identifier = url.split("/")[-2]
            return identifier, f"https://www.hellowork.com/fr-fr/emplois/{identifier}.html"

        offers = await resolve_alert_offers(alert, resolver=resolver)
        self.assertEqual(len(alert.tracking_urls), 10)
        self.assertEqual(len(offers), 10)
        self.assertEqual(offers[0][0], "1")

    async def test_accepts_forward_from_personal_sender_without_original_auth(self):
        raw = alert_bytes().replace(b"dkim=pass", b"dkim=fail")
        raw = raw.replace(
            b"Hellowork notifications <notification@emails.hellowork.com>",
            b"Candidate <candidate@example.com>",
        )
        alert = parse_hellowork_alert(raw)
        self.assertEqual(len(alert.tracking_urls), 10)

    async def test_accepts_manual_forward_as_attachment(self):
        original = BytesParser(policy=policy.default).parsebytes(alert_bytes())
        forwarded = EmailMessage()
        forwarded["From"] = "candidate@example.com"
        forwarded["To"] = "bot@example.com"
        forwarded["Subject"] = "Fwd: HelloWork jobs"
        forwarded.set_content("Original HelloWork alert attached.")
        forwarded.add_attachment(original)

        alert = parse_hellowork_alert(forwarded.as_bytes())

        self.assertEqual(len(alert.tracking_urls), 10)

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
    def test_legacy_database_adds_parser_revision_column(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            connection = sqlite3.connect(db)
            connection.execute(
                """CREATE TABLE inbound_email_messages (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       mailbox_key TEXT NOT NULL,
                       uid_validity TEXT NOT NULL,
                       uid TEXT NOT NULL,
                       message_id_hash TEXT NOT NULL,
                       content_hash TEXT NOT NULL UNIQUE,
                       status TEXT NOT NULL,
                       discovered_count INTEGER NOT NULL DEFAULT 0,
                       duplicate_count INTEGER NOT NULL DEFAULT 0,
                       last_error TEXT NOT NULL DEFAULT '',
                       first_seen_at TEXT NOT NULL,
                       handled_at TEXT,
                       UNIQUE(mailbox_key, uid_validity, uid)
                   )"""
            )
            connection.commit()
            connection.close()

            replayable_rejected_uids(
                db, mailbox_key="bot", uid_validity="7", parser_revision=2,
            )
            connection = sqlite3.connect(db)
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(inbound_email_messages)"
                )
            }
            connection.close()
            self.assertIn("parser_revision", columns)

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

    def test_existing_schema_migrates_and_rejected_receipt_can_be_queued(self):
        offers = (("1", "https://www.hellowork.com/fr-fr/emplois/1.html"),)
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            record_rejected_email(
                db, mailbox_key="bot", uid_validity="7", uid="9",
                message_id="m1", raw_message=b"raw",
                reason="no HelloWork tracking links found",
            )
            self.assertEqual(
                replayable_rejected_uids(
                    db, mailbox_key="bot", uid_validity="7", parser_revision=2,
                ),
                ("9",),
            )
            self.assertEqual(
                record_email_offers(
                    db, mailbox_key="bot", uid_validity="7", uid="9",
                    message_id="m1", raw_message=b"raw", offers=offers,
                    parser_revision=2,
                ),
                (1, 0, False),
            )
            connection = sqlite3.connect(db)
            row = connection.execute(
                "SELECT status, parser_revision FROM inbound_email_messages"
            ).fetchone()
            connection.close()
            self.assertEqual(row, ("queued", 2))

    def test_replay_is_bounded_and_stale_uidvalidity_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            for index in range(30):
                record_rejected_email(
                    db, mailbox_key="bot", uid_validity="7", uid=str(index),
                    message_id=str(index), raw_message=f"raw-{index}".encode(),
                    reason="no valid HelloWork offer links found",
                )
            self.assertEqual(
                len(replayable_rejected_uids(
                    db, mailbox_key="bot", uid_validity="7",
                    parser_revision=2, limit=100,
                )),
                25,
            )
            self.assertEqual(
                close_stale_rejected_emails(
                    db, mailbox_key="bot", current_uid_validity="8",
                    parser_revision=2,
                ),
                30,
            )
            self.assertEqual(
                replayable_rejected_uids(
                    db, mailbox_key="bot", uid_validity="7", parser_revision=2,
                ),
                (),
            )


class EmailReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_automatically_fetches_and_recovers_seen_rejection(self):
        raw = production_alert_bytes()

        class Inbox:
            def __init__(self):
                self.marked = []

            def unread(self):
                return "7", ()

            def fetch_uids(self, uids):
                self.requested = uids
                return "7", (InboxMessage("9", raw),), ()

            def mark_seen(self, uid):
                self.marked.append(uid)

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            record_rejected_email(
                db, mailbox_key="bot", uid_validity="7", uid="9",
                message_id="m1", raw_message=raw,
                reason="no HelloWork tracking links found",
            )
            inbox = Inbox()
            bot = AsyncMock()
            with (
                patch.object(email_ingest.config, "JOBS_DB_PATH", db),
                patch.object(email_ingest.config, "HELLOWORK_IMAP_USERNAME", "bot"),
            ):
                with self.assertLogs("jobbot", level="INFO") as captured:
                    handled = await email_ingest.ingest_email_once(bot, inbox=inbox)
            self.assertEqual(handled, 1)
            self.assertEqual(inbox.requested, ("9",))
            self.assertEqual(inbox.marked, [])
            self.assertEqual(get_offer(db, "81835625")["status"], "pending")
            self.assertEqual(
                replayable_rejected_uids(
                    db, mailbox_key="bot", uid_validity="7", parser_revision=2,
                ),
                (),
            )
            logs = "\n".join(captured.output)
            self.assertNotIn("bot@example.invalid", logs)
            self.assertNotIn("/clic/", logs)

    async def test_missing_replay_message_is_closed_once(self):
        class Inbox:
            def unread(self):
                return "7", ()

            def fetch_uids(self, uids):
                return "7", (), uids

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "jobs.db"
            record_rejected_email(
                db, mailbox_key="bot", uid_validity="7", uid="9",
                message_id="m1", raw_message=b"missing",
                reason="no HelloWork tracking links found",
            )
            with (
                patch.object(email_ingest.config, "JOBS_DB_PATH", db),
                patch.object(email_ingest.config, "HELLOWORK_IMAP_USERNAME", "bot"),
            ):
                handled = await email_ingest.ingest_email_once(
                    AsyncMock(), inbox=Inbox()
                )
            self.assertEqual(handled, 0)
            self.assertEqual(
                replayable_rejected_uids(
                    db, mailbox_key="bot", uid_validity="7", parser_revision=2,
                ),
                (),
            )


if __name__ == "__main__":
    unittest.main()

