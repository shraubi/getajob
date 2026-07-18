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
                repository="shraubi/getajob", token="secret", post=post
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
                repository="shraubi/getajob", token="secret", post=post
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

            outbox.publish_pending(repository="shraubi/getajob", token="secret", post=post)
            self.assertEqual(calls[0]["title"], "[Ralph] Application blocked: required fields")
            self.assertIn("missing from `applicant.json`", calls[0]["body"])
            self.assertNotIn("seen the similar bug", calls[0]["body"].casefold())


if __name__ == "__main__":
    unittest.main()
