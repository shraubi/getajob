import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import asyncio
import httpx

from ralph.github_issue import sync_issue
from ralph.rating import RatingReport, StageRating, failure_fingerprint
from ralph.discover import extract_job_urls
from ralph.store import record_report, render_issue
from ralph.telegram_flow import ObservedMessage, review_bot_output


class RalphFirstLoopTests(unittest.TestCase):
    def test_github_sync_creates_issue_for_new_fingerprint(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=[], request=request)
            return httpx.Response(201, json={"number": 24, "html_url": "https://github.com/o/r/issues/24"}, request=request)

        payload = render_issue(self._report(), "run-123", self._report().failures[0])
        result = asyncio.run(sync_issue(
            payload,
            repository="o/r",
            token="test-token",
            transport=httpx.MockTransport(handler),
        ))
        self.assertEqual(result, {"number": 24, "html_url": "https://github.com/o/r/issues/24", "created": True})
        self.assertEqual([request.method for request in requests], ["GET", "POST"])

    def test_reviews_full_telegram_failure_chain(self):
        messages = (
            ObservedMessage("Fetching and parsing the linked job page..."),
            ObservedMessage("This role does not match any of the available resumes, so nothing will be sent."),
        )
        report = review_bot_output(
            "https://hirify.me/jobs/720229-dataops-engineer",
            messages,
            expected_direction="data_engineering",
        )
        # New scoring: parser(40) + classification(0) + applicant_profile(20) + application(0) = 60
        self.assertEqual(report.score, 60)
        # applicant_profile now passes by default in telegram_flow (20 points)
        # classification fails (expected data_engineering, got other or unsupported)
        # application fails (no resume, no application result)
        self.assertEqual([failure.stage for failure in report.failures], ["classification", "application"])

    def test_reviews_successful_telegram_chain_without_clicking(self):
        messages = (
            ObservedMessage("Direction: data_engineering\nRole: DataOps Engineer\nCompany: Example"),
            ObservedMessage("Selected resume", has_document=True),
            ObservedMessage("Recruiter message:\n\nHello", buttons=("Apply with resume", "Skip")),
        )
        report = review_bot_output("https://hirify.me/jobs/1", messages, expected_direction="data_engineering")
        # New scoring: parser(40) + classification(30) + applicant_profile(20) + application(20) = 110
        self.assertEqual(report.score, 110)
        self.assertEqual(report.status, "passed")

    def test_reviews_applicant_profile_error(self):
        messages = (
            ObservedMessage("Direction: data_engineering\nRole: DataOps Engineer\nCompany: Example"),
            ObservedMessage("Selected resume", has_document=True),
            ObservedMessage("Application failed: Applicant profile is missing required fields: name, phone, urls[LinkedIn]"),
        )
        report = review_bot_output("https://hirify.me/jobs/1", messages, expected_direction="data_engineering")
        # applicant_profile should fail with 0 points
        # parser(40) + classification(30) + applicant_profile(0) + application(0) = 70
        self.assertEqual(report.score, 70)
        self.assertEqual([failure.stage for failure in report.failures], ["applicant_profile", "application"])
        # Check that the applicant_profile failure contains the right evidence
        profile_failure = next(f for f in report.failures if f.stage == "applicant_profile")
        self.assertIn("missing required fields", profile_failure.summary)
        self.assertEqual(profile_failure.evidence["error_type"], "applicant_profile_missing_fields")

    def test_extracts_unique_hirify_job_urls(self):
        html = '<a href="/jobs/10-python">one</a><a href="https://hirify.me/jobs/10-python?utm=x">dup</a><a href="/about">no</a>'
        self.assertEqual(extract_job_urls(html, "https://hirify.me/"), ["https://hirify.me/jobs/10-python"])

    def _report(self):
        stages = (
            StageRating("parser", True, 40, 40, "Required vacancy fields were parsed", {}),
            StageRating("classification", True, 30, 30, "Classified as backend_python", {}),
            StageRating(
                "applicant_profile", True, 20, 20, "Applicant profile stage requires Telegram flow for validation", {}
            ),
            StageRating(
                "application", False, 0, 20,
                "Application target is a same-page JavaScript flow with no discoverable static form",
                {"apply_url": "https://jobs.example/42", "same_page_target": True},
            ),
        )
        return RatingReport(
            "https://jobs.example/42", "jobs.example", "Senior Python Engineer",
            "Example", "backend_python", 90, "failed", stages,
        )

    def test_records_and_deduplicates_failure(self):
        report = self._report()
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "ralph.db"
            _, first = record_report(db_path, report)
            _, second = record_report(db_path, report)
            self.assertEqual(first, second)
            connection = sqlite3.connect(db_path)
            try:
                row = connection.execute(
                    "SELECT occurrences, stage FROM ralph_failures WHERE fingerprint=?", (first[0],)
                ).fetchone()
            finally:
                connection.close()
        self.assertEqual(row, (2, "application"))

    def test_issue_is_deterministic_and_contains_reproduction(self):
        report = self._report()
        failure = report.failures[0]
        issue = render_issue(report, "run-123", failure)
        self.assertIn("[Ralph][application]", issue["title"])
        self.assertIn("python -m ralph.first_loop", issue["body"])
        self.assertIn(failure_fingerprint(report, failure), issue["body"])
        self.assertNotIn("resume.pdf", json.dumps(issue))


if __name__ == "__main__":
    unittest.main()
