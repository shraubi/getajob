"""Deduplicated GitHub issue delivery for normalized Ralph findings."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

from .models import Finding, ReviewReport

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ralph_github_issue_outbox (
    fingerprint TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    issue_number INTEGER,
    issue_url TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    last_attempt_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
_SEVERITY = {"low": 1, "medium": 2, "high": 3}
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class IssueDelivery:
    fingerprint: str
    title: str
    body: str


def _fingerprint(report: ReviewReport, finding: Finding) -> str:
    identity = json.dumps(
        {
            "peer": report.peer_key,
            "interaction": finding.interaction_id,
            "rule": finding.rule_id,
            "message_ids": finding.message_ids,
        },
        sort_keys=True,
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _display_value(value: object) -> str:
    return str(value).replace("_", " ").strip()


def _issue_explanation(finding: Finding) -> tuple[str, str, str, str]:
    """Turn normalized evidence into an actionable, human-readable bug report."""
    evidence = finding.evidence
    role = str(evidence.get("title") or "this vacancy").strip()

    if finding.rule_id == "application_blocked":
        blockers = evidence.get("blocker_types")
        blocker = str(blockers[0]) if isinstance(blockers, list) and blockers else "unknown error"
        event_type = str(evidence.get("event_type") or "application_failed")
        if blocker == "required_fields":
            cause = "the ATS requires answers that are missing from `applicant.json`"
            next_step = "Record which required questions were missing and add supported answers to the applicant profile."
        elif blocker == "UnsafeUrlError":
            cause = "URL safety validation rejected the vacancy or one of its redirects"
            next_step = "Inspect the vacancy's redirect chain and the URL allow-list to find the exact rejected host or address."
        elif blocker == "missing_saved_job":
            cause = "the application button referred to a job that was no longer present in the local job store"
            next_step = "Check why the saved job cannot be found from the callback prefix and preserve it until the action expires."
        else:
            cause = f"the application flow raised `{blocker}`"
            next_step = f"Reproduce the application and inspect the `{blocker}` failure at the `{event_type}` stage."
        stage = "load the vacancy" if event_type == "job_fetch_failed" else "complete the application"
        return (
            f"Jobbot could not {stage} because {cause}.",
            f"Jobbot should {stage} or give the user a precise, actionable manual fallback.",
            next_step,
            f"Application blocked: {_display_value(blocker)}",
        )

    if finding.rule_id == "resume_preview_missing":
        direction = _display_value(evidence.get("direction") or "unknown")
        return (
            f"Jobbot accepted {role}, but did not attach a resume preview. The recorded resume direction was `{direction}`.",
            "Every accepted vacancy should produce the selected resume document before an application action is offered.",
            "Check direction selection and resume-file lookup for this vacancy, including whether the expected PDF exists.",
            f"Resume preview missing for {role}",
        )

    if finding.rule_id == "application_path_missing":
        return (
            f"Jobbot produced a resume preview for {role}, but offered no application button or recruiter contact.",
            "A previewed vacancy should include a usable application, email, Telegram, or manual handoff action.",
            "Inspect the parsed apply URL and contact fields, then preserve the reason when no action can be built.",
            f"No application action for {role}",
        )

    if finding.rule_id in {"supported_role_rejected", "support_role_misclassified"}:
        expected = _display_value(evidence.get("expected_direction") or "tech_support")
        return (
            f"Jobbot rejected {role} as unsupported even though Ralph identified the supported direction `{expected}`.",
            f"The vacancy should be classified as `{expected}` and continue to resume selection.",
            "Review the direction scoring threshold and the title/description signals that led to the rejection.",
            f"Supported role rejected: {role}",
        )

    if finding.rule_id == "unclassified_role_rejected":
        return (
            f"Jobbot rejected {role}, and every recorded supported-direction score was zero. Ralph cannot tell whether this is a classifier gap or a correct rejection.",
            "The issue should identify the missing classifier signal; if the role is intentionally unsupported, Ralph should not report it as a bug.",
            "Decide whether this role belongs to a supported direction. If it does, add the missing scoring signals and a regression test.",
            f"Unclassified rejected role: {role}",
        )

    if finding.rule_id in {"telegram_throttled", "telegram_queue_missing"}:
        reason = _display_value(evidence.get("reason") or "unknown limit")
        queued = bool(evidence.get("queue_present"))
        return (
            f"Telegram blocked the send because of `{reason}`; the application was {'queued' if queued else 'not queued'} for retry.",
            "A rate-limited application should be retained and retried after the cooldown instead of being lost.",
            "Persist the pending send with its retry time and verify the worker resumes it after the cooldown.",
            f"Telegram send not safely deferred: {reason}",
        )

    return (
        finding.summary.rstrip(".") + ".",
        "The interaction should complete without this condition.",
        "Reproduce the interaction using the source and event identifiers below, then trace the failing stage.",
        _display_value(finding.rule_id).capitalize(),
    )


def _issue_content(report: ReviewReport, finding: Finding) -> tuple[str, str]:
    problem, expected, next_step, readable_title = _issue_explanation(finding)
    title = f"[Ralph] {readable_title}"[:256]
    fingerprint = _fingerprint(report, finding)
    urls = finding.evidence.get("urls")
    if not isinstance(urls, list):
        urls = []
    lines = [
        "## What is wrong",
        "",
        problem,
        "",
        "## Expected behavior",
        "",
        expected,
        "",
        "## Where to investigate",
        "",
        next_step,
        "",
        "## Occurrence",
        "",
        f"- Interaction: `{finding.interaction_id}`",
        f"- Event IDs: {', '.join(str(value) for value in finding.message_ids)}",
        f"- Ralph rule: `{finding.rule_id}` ({finding.severity})",
    ]
    for url in urls:
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            lines.append(f"- Source: {url}")
    lines.extend(
        (
            "",
            "<details>",
            "<summary>Normalized diagnostic evidence</summary>",
            "",
            "```json",
            json.dumps(finding.evidence, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "</details>",
            "",
            f"<!-- ralph-finding:{fingerprint} -->",
        )
    )
    return title, "\n".join(lines)


class GitHubIssueOutbox:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.execute(_SCHEMA)
        return connection

    def enqueue_report(self, report: ReviewReport, *, min_severity: str = "medium") -> int:
        threshold = _SEVERITY.get(min_severity, _SEVERITY["medium"])
        now = datetime.now(timezone.utc).isoformat()
        queued = 0
        connection = self._connect()
        try:
            for finding in report.findings:
                if _SEVERITY.get(finding.severity, 0) < threshold:
                    continue
                fingerprint = _fingerprint(report, finding)
                title, body = _issue_content(report, finding)
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO ralph_github_issue_outbox (
                        fingerprint, title, body, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (fingerprint, title, body, now, now),
                )
                queued += cursor.rowcount
            connection.commit()
            return queued
        finally:
            connection.close()

    def publish_pending(
        self,
        *,
        repository: str,
        token: str,
        retry_seconds: int = 300,
        post: Callable[..., object] = httpx.post,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not token or not _REPOSITORY_RE.fullmatch(repository):
            return (), ()
        retry_before = (
            datetime.now(timezone.utc) - timedelta(seconds=retry_seconds)
        ).isoformat()
        connection = self._connect()
        created: list[str] = []
        failed: list[str] = []
        try:
            rows = connection.execute(
                """SELECT fingerprint, title, body
                   FROM ralph_github_issue_outbox
                   WHERE status='pending'
                     AND (last_attempt_at='' OR last_attempt_at <= ?)
                   ORDER BY created_at LIMIT 10""",
                (retry_before,),
            ).fetchall()
            for fingerprint, title, body in rows:
                attempted_at = datetime.now(timezone.utc).isoformat()
                try:
                    response = post(
                        f"https://api.github.com/repos/{repository}/issues",
                        headers={
                            "Accept": "application/vnd.github+json",
                            "Authorization": f"Bearer {token}",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                        json={"title": title, "body": body},
                        timeout=20.0,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    issue_url = str(payload["html_url"])
                    issue_number = int(payload["number"])
                    connection.execute(
                        """UPDATE ralph_github_issue_outbox
                           SET status='created', issue_number=?, issue_url=?,
                               attempts=attempts+1, last_error='',
                               last_attempt_at=?, updated_at=?
                           WHERE fingerprint=?""",
                        (issue_number, issue_url, attempted_at, attempted_at, fingerprint),
                    )
                    created.append(issue_url)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:500]
                    connection.execute(
                        """UPDATE ralph_github_issue_outbox
                           SET attempts=attempts+1, last_error=?,
                               last_attempt_at=?, updated_at=?
                           WHERE fingerprint=?""",
                        (error, attempted_at, attempted_at, fingerprint),
                    )
                    failed.append(error)
            connection.commit()
            return tuple(created), tuple(failed)
        finally:
            connection.close()
