import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ralph.analyzer import analyze_interactions, group_interactions
from ralph.history import (
    RalphHistoryError,
    marker_run_id,
    parse_since,
    read_history,
    select_latest_marker,
)
from ralph.models import ChatMessage, ReviewReport
from ralph.store import RalphStore, write_report

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def message(mid, text="", *, outgoing=False, seconds=0, document=False, buttons=()):
    return ChatMessage(
        id=mid, date=NOW + timedelta(seconds=seconds), outgoing=outgoing,
        text=text, has_document=document, buttons=buttons,
    )


class RawMessage:
    def __init__(self, item):
        self.id = item.id
        self.date = item.date
        self.out = item.outgoing
        self.raw_text = item.text
        self.document = object() if item.has_document else None
        self.buttons = [[SimpleNamespace(text=value) for value in item.buttons]] if item.buttons else None
        self.edit_date = None
        self.reply_to_msg_id = None


class AsyncItems:
    def __init__(self, items):
        self.items = list(items)

    def __aiter__(self):
        self.iterator = iter(self.items)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeClient:
    def __init__(self, markers, history):
        self.markers = markers
        self.history = history
        self.calls = []

    def iter_messages(self, entity, **kwargs):
        self.calls.append(kwargs)
        items = self.markers if kwargs.get("search") else self.history
        limit = kwargs.get("limit")
        return AsyncItems([RawMessage(item) for item in items[:limit]])


class RalphMarkerTests(unittest.TestCase):
    def test_selects_latest_exact_outgoing_marker(self):
        older = message(5, "https://job\n\nRalph-Run: " + "a" * 32, outgoing=True)
        newer = message(9, "Ralph-Run: " + "b" * 32, outgoing=True)
        incoming = message(12, "Ralph-Run: " + "c" * 32)
        partial = message(15, "prefix Ralph-Run: " + "d" * 32, outgoing=True)
        selected = select_latest_marker((older, newer, incoming, partial))
        self.assertEqual(selected.message.id, 9)
        self.assertEqual(selected.run_id, "b" * 32)
        self.assertIsNone(marker_run_id(incoming))
        self.assertIsNone(marker_run_id(partial))

    def test_defaults_to_most_recent_messages_without_marker_or_since(self):
        client = FakeClient([], [message(3, "hello"), message(2, "older")])
        result = asyncio.run(read_history(
            client, object(), peer_key="bot", checkpoint_message_id=None,
            since=None, replay_latest_run=False,
        ))
        self.assertEqual([item.id for item in result.messages], [2, 3])

    def test_checkpoint_and_replay_precedence(self):
        marker = message(10, "Ralph-Run: " + "a" * 32, outgoing=True)
        client = FakeClient([marker], [message(21, "response")])
        result = asyncio.run(read_history(
            client, object(), peer_key="bot", checkpoint_message_id=20,
            since=None, replay_latest_run=False,
        ))
        self.assertEqual(result.boundary_message_id, 20)
        self.assertIsNone(result.seed_request)

        client = FakeClient([marker], [message(11, "response")])
        replay = asyncio.run(read_history(
            client, object(), peer_key="bot", checkpoint_message_id=20,
            since=None, replay_latest_run=True,
        ))
        self.assertEqual(replay.boundary_message_id, 10)
        self.assertEqual(replay.seed_request.id, 10)

    def test_since_fallback_and_iso_parsing(self):
        since = parse_since("2026-07-18T10:00:00Z")
        client = FakeClient([], [message(3, "hello")])
        result = asyncio.run(read_history(
            client, object(), peer_key="bot", checkpoint_message_id=None,
            since=since, replay_latest_run=False,
        ))
        self.assertEqual(result.messages[0].id, 3)
        self.assertEqual(client.calls[-1]["offset_date"], since)

    def test_since_overrides_marker_and_checkpoint(self):
        since = parse_since("2026-07-18T10:00:00Z")
        marker = message(10, "Ralph-Run: " + "a" * 32, outgoing=True)
        client = FakeClient([marker], [message(3, "hello")])
        result = asyncio.run(read_history(
            client, object(), peer_key="bot", checkpoint_message_id=20,
            since=since, replay_latest_run=False,
        ))
        self.assertEqual(result.boundary_message_id, 0)
        self.assertEqual(result.messages[0].id, 3)
        self.assertEqual(client.calls[-1]["offset_date"], since)
        self.assertNotIn("min_id", client.calls[-1])

    def test_paginates_thirty_messages_without_skipping(self):
        marker = message(1, "Ralph-Run: " + "a" * 32, outgoing=True)
        history = [message(index, "response") for index in range(2, 33)]
        result = asyncio.run(read_history(
            FakeClient([marker], history), object(), peer_key="bot",
            checkpoint_message_id=None, since=None, replay_latest_run=False,
        ))
        self.assertEqual(len(result.messages), 30)
        self.assertEqual(result.messages[0].id, 2)
        self.assertEqual(result.messages[-1].id, 31)
        self.assertTrue(result.has_more)


class RalphAnalyzerTests(unittest.TestCase):
    def analyze(self, request_text, responses):
        request = message(1, request_text, outgoing=True)
        interactions = group_interactions(tuple(responses), seed_request=request)
        return {finding.rule_id for finding in analyze_interactions(interactions)}

    def test_groups_interactions_chronologically(self):
        messages = (
            message(2, "first response"),
            message(3, "next job", outgoing=True),
            message(4, "second response"),
        )
        groups = group_interactions(messages, seed_request=message(1, "first job", outgoing=True))
        self.assertEqual([group.id for group in groups], ["1", "3"])
        self.assertEqual(groups[0].responses[0].id, 2)

    def test_flags_support_misclassification(self):
        rules = self.analyze(
            "Title: Customer Support Specialist",
            [message(2, "This role does not match any of the available resumes.")],
        )
        self.assertIn("support_role_misclassified", rules)

    def test_flags_site_blocker_and_missing_path(self):
        blocked = self.analyze(
            "https://jobs.example/42\nTitle: Python Engineer",
            [message(2, "Direction: backend_python\nApplication failed: CAPTCHA required", document=True)],
        )
        self.assertIn("application_blocked", blocked)
        missing = self.analyze(
            "Title: Python Engineer",
            [message(2, "Direction: backend_python\nRecruiter message:", document=True)],
        )
        self.assertIn("application_path_missing", missing)

    def test_flags_throttle_and_queue_gap(self):
        rules = self.analyze(
            "Title: Technical Support",
            [message(2, "Telegram send paused until tomorrow: minimum interval")],
        )
        self.assertIn("telegram_throttled", rules)
        self.assertIn("telegram_queue_missing", rules)

    def test_ignores_missing_company_and_expired_page(self):
        company = self.analyze("Title: Python Engineer", [message(2, "Company: Unknown company")])
        self.assertNotIn("missing_company", company)
        expired = self.analyze(
            "https://jobs.example/expired",
            [message(2, "Job page is no longer available (HTTP 404)")],
        )
        self.assertEqual(expired, set())

    def test_flags_missing_and_delayed_response(self):
        missing = self.analyze("Title: Python Engineer", [])
        self.assertIn("bot_response_missing", missing)
        delayed = self.analyze(
            "Title: Python Engineer",
            [message(2, "Direction: backend_python", seconds=31)],
        )
        self.assertIn("bot_response_delayed", delayed)


class RalphStoreTests(unittest.TestCase):
    def test_persists_findings_without_transcript(self):
        interaction = group_interactions(
            (message(2, "Telegram sending is disabled"),),
            seed_request=message(1, "private vacancy text", outgoing=True),
        )
        findings = analyze_interactions(interaction)
        report = ReviewReport(
            id="run", peer_key="bot", marker_message_id=1,
            marker_run_id="a" * 32, start_message_id=1, end_message_id=2,
            analyzed_messages=1, source_urls=("https://jobs.example/1",),
            has_more=True, findings=findings, created_at=NOW.isoformat(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "ralph.db"
            output = root / "report.json"
            write_report(report, output)
            RalphStore(db).save_review(report, output)
            serialized = output.read_text(encoding="utf-8")
            database_bytes = db.read_bytes()
            self.assertNotIn("private vacancy text", serialized)
            self.assertNotIn(b"private vacancy text", database_bytes)
            data = json.loads(serialized)
            self.assertEqual(data["review_run_id"], "run")
            self.assertEqual(data["source_urls"], ["https://jobs.example/1"])
            self.assertTrue(data["has_more"])
            checkpoint = RalphStore(db).get_checkpoint("bot")
            self.assertEqual(checkpoint.last_message_id, 2)
            connection = sqlite3.connect(db)
            try:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM ralph_review_findings"
                ).fetchone()[0], len(findings))
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
