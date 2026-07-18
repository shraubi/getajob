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


def _issue_content(report: ReviewReport, finding: Finding) -> tuple[str, str]:
    role_title = str(finding.evidence.get("title") or "").strip()
    suffix = f": {role_title}" if role_title else ""
    title = f"[Ralph] {finding.rule_id}{suffix}"[:256]
    fingerprint = _fingerprint(report, finding)
    urls = finding.evidence.get("urls")
    if not isinstance(urls, list):
        urls = []
    lines = [
        f"Ralph detected **{finding.rule_id}** ({finding.severity}).",
        "",
        finding.summary,
        "",
        f"- Interaction: `{finding.interaction_id}`",
        f"- Event IDs: {', '.join(str(value) for value in finding.message_ids)}",
    ]
    for url in urls:
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            lines.append(f"- Source: {url}")
    lines.extend(
        (
            "",
            "Normalized evidence:",
            "```json",
            json.dumps(finding.evidence, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
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
