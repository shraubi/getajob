import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ralph.github_issues import GitHubIssueOutbox
from ralph.models import Finding, ReviewReport


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"number": 54, "html_url": "https://github.com/shraubi/getajob/issues/54"}


class FakeSearchResponse:
    def __init__(self, items=()):
        self.items = items

    def raise_for_status(self):
        return None

    def json(self):
        return {"items": list(self.items)}


class RalphGitHubIssueTests(unittest.TestCase):
    def report(self):
        finding = Finding(
            rule_id="supported_role_rejected", severity="high",
            summary="A supported role was rejected",
            interaction_id="1:7", message_ids=(7,),
            timestamps=("2026-07-18T16:46:18+00:00",),
            evidence={
                "title": "Vibe Coder",
                "expected_direction": "ml_engineering",
                "urls": ["https://adaptify.ai/jobs/vibe-coder"],
            },
        )
        return ReviewReport(
            id="review", peer_key="jobbot-events",
            marker_message_id=None, marker_run_id=None,
            start_message_id=6, end_message_id=7,
            analyzed_messages=1, source_urls=tuple(finding.evidence["urls"]),
            has_more=False, findings=(finding,),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def test_deduplicates_and_publishes_normalized_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = GitHubIssueOutbox(Path(directory) / "ralph.db")
            report = self.report()
            self.assertEqual(outbox.enqueue_report(report), 1)
            self.assertEqual(outbox.enqueue_report(report), 0)
            calls = []

            def post(url, **kwargs):
                calls.append((url, kwargs))
                return FakeResponse()

            created, failed = outbox.publish_pending(
                repository="shraubi/getajob", token="secret",
                get=lambda *args, **kwargs: FakeSearchResponse(), post=post,
            )
            self.assertEqual(created, ("https://github.com/shraubi/getajob/issues/54",))
            self.assertEqual(failed, ())
            self.assertEqual(len(calls), 1)
            self.assertNotIn("secret", calls[0][1]["json"]["body"])
            self.assertIn("Vibe Coder", calls[0][1]["json"]["title"])
            body = calls[0][1]["json"]["body"]
            self.assertIn("## What is wrong", body)
            self.assertIn("rejected Vibe Coder as unsupported", body)
            self.assertIn("## Expected behavior", body)
            self.assertIn("## Where to investigate", body)
            again, _ = outbox.publish_pending(
                repository="shraubi/getajob", token="secret",
                get=lambda *args, **kwargs: FakeSearchResponse(), post=post,
            )
            self.assertEqual(again, ())
            self.assertEqual(len(calls), 1)

    def test_application_blocker_explains_the_specific_failure(self):
        finding = Finding(
            rule_id="application_blocked", severity="high",
            summary="The application path ended in a known blocker",
            interaction_id="1:8", message_ids=(8,), timestamps=("now",),
            evidence={
                "blocker_types": ["required_fields"],
                "event_type": "application_failed",
                "urls": ["https://jobs.example/8"],
            },
        )
        report = self.report()
        report = ReviewReport(**{**report.__dict__, "findings": (finding,)})
        with tempfile.TemporaryDirectory() as directory:
            outbox = GitHubIssueOutbox(Path(directory) / "ralph.db")
            outbox.enqueue_report(report)
            calls = []

            def post(url, **kwargs):
                calls.append(kwargs["json"])
                return FakeResponse()

            outbox.publish_pending(
                repository="shraubi/getajob", token="secret",
                get=lambda *args, **kwargs: FakeSearchResponse(), post=post,
            )
            self.assertEqual(calls[0]["title"], "[Ralph] Application blocked: required fields")
            self.assertIn("missing from `applicant.json`", calls[0]["body"])
            self.assertNotIn("seen the similar bug", calls[0]["body"].casefold())

    def test_deduplicates_repeated_occurrences_with_the_same_evidence(self):
        first = self.report()
        repeated_finding = Finding(
            **{
                **first.findings[0].__dict__,
                "interaction_id": "1:99",
                "message_ids": (99,),
                "timestamps": ("2026-07-19T10:00:00+00:00",),
            }
        )
        repeated = ReviewReport(
            **{
                **first.__dict__,
                "id": "later-review",
                "findings": (repeated_finding,),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            outbox = GitHubIssueOutbox(Path(directory) / "ralph.db")
            self.assertEqual(outbox.enqueue_report(first), 1)
            self.assertEqual(outbox.enqueue_report(repeated), 0)

    def test_collapses_telegram_rule_aliases_and_reason_formatting(self):
        base = Finding(
            rule_id="telegram_throttled", severity="medium",
            summary="Telegram throttled",
            interaction_id="1:10", message_ids=(10,), timestamps=("now",),
            evidence={
                "reason": "Telegram PeerFlood",
                "queue_present": False,
                "urls": ["https://jobs.example/10"],
            },
        )
        duplicate = Finding(
            **{
                **base.__dict__,
                "rule_id": "telegram_queue_missing",
                "severity": "high",
                "interaction_id": "1:11",
                "message_ids": (11,),
                "evidence": {
                    **base.evidence,
                    "reason": "peer_flood",
                },
            }
        )
        report = self.report()
        report = ReviewReport(**{**report.__dict__, "findings": (base, duplicate)})
        with tempfile.TemporaryDirectory() as directory:
            outbox = GitHubIssueOutbox(Path(directory) / "ralph.db")
            self.assertEqual(outbox.enqueue_report(report), 1)

    def test_keeps_different_diagnostic_evidence_separate(self):
        first = self.report()
        changed_finding = Finding(
            **{
                **first.findings[0].__dict__,
                "interaction_id": "1:99",
                "message_ids": (99,),
                "evidence": {
                    **first.findings[0].evidence,
                    "expected_direction": "backend_python",
                },
            }
        )
        changed = ReviewReport(
            **{
                **first.__dict__,
                "id": "changed-review",
                "findings": (changed_finding,),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            outbox = GitHubIssueOutbox(Path(directory) / "ralph.db")
            self.assertEqual(outbox.enqueue_report(first), 1)
            self.assertEqual(outbox.enqueue_report(changed), 1)

    def test_source_url_does_not_turn_the_same_error_into_a_new_issue(self):
        first = self.report()
        repeated_finding = Finding(
            **{
                **first.findings[0].__dict__,
                "interaction_id": "1:100",
                "message_ids": (100,),
                "evidence": {
                    **first.findings[0].evidence,
                    "urls": ["https://another.example/same-role"],
                },
            }
        )
        repeated = ReviewReport(
            **{
                **first.__dict__,
                "id": "another-source",
                "findings": (repeated_finding,),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            outbox = GitHubIssueOutbox(Path(directory) / "ralph.db")
            self.assertEqual(outbox.enqueue_report(first), 1)
            self.assertEqual(outbox.enqueue_report(repeated), 0)

    def test_application_blocker_deduplicates_across_vacancies(self):
        def blocked(url: str, interaction: str, event_id: int) -> Finding:
            return Finding(
                rule_id="application_blocked", severity="high",
                summary="The application path ended in a known blocker",
                interaction_id=interaction, message_ids=(event_id,),
                timestamps=("now",),
                evidence={
                    "blocker_types": ["required_fields"],
                    "event_type": "application_failed",
                    "title": "",
                    "urls": [url],
                },
            )

        report = self.report()
        first = ReviewReport(
            **{
                **report.__dict__,
                "findings": (blocked("https://jobs.example/1", "1:1", 1),),
            }
        )
        second = ReviewReport(
            **{
                **report.__dict__,
                "id": "second-block",
                "findings": (blocked("https://jobs.example/2", "1:2", 2),),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            outbox = GitHubIssueOutbox(Path(directory) / "ralph.db")
            self.assertEqual(outbox.enqueue_report(first), 1)
            self.assertEqual(outbox.enqueue_report(second), 0)

    def test_remote_issue_prevents_duplicate_after_local_database_loss(self):
        report = self.report()
        with tempfile.TemporaryDirectory() as directory:
            first = GitHubIssueOutbox(Path(directory) / "first.db")
            first.enqueue_report(report)
            posted = []

            def post(url, **kwargs):
                posted.append(kwargs["json"])
                return FakeResponse()

            first.publish_pending(
                repository="shraubi/getajob", token="secret",
                get=lambda *args, **kwargs: FakeSearchResponse(), post=post,
            )
            self.assertEqual(len(posted), 1)

            rebuilt = GitHubIssueOutbox(Path(directory) / "rebuilt.db")
            rebuilt.enqueue_report(report)
            existing = {
                "number": 54,
                "html_url": "https://github.com/shraubi/getajob/issues/54",
                "body": posted[0]["body"],
            }
            created, failed = rebuilt.publish_pending(
                repository="shraubi/getajob", token="secret",
                get=lambda *args, **kwargs: FakeSearchResponse((existing,)),
                post=post,
            )
            self.assertEqual(created, ())
            self.assertEqual(failed, ())
            self.assertEqual(len(posted), 1)


if __name__ == "__main__":
    unittest.main()
