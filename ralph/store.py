"""SQLite persistence and deterministic GitHub issue rendering for Ralph."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .rating import RatingReport, StageRating, failure_fingerprint


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ralph_runs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    score INTEGER NOT NULL,
    status TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ralph_failures (
    fingerprint TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    stage TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    issue_number INTEGER,
    status TEXT NOT NULL DEFAULT 'queued',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
"""


def record_report(db_path: Path, report: RatingReport) -> tuple[str, list[str]]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    run_id = uuid.uuid4().hex
    fingerprints: list[str] = []
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO ralph_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, report.url, report.domain, report.score, report.status, json.dumps(report.to_dict(), ensure_ascii=False), now),
        )
        for failure in report.failures:
            fingerprint = failure_fingerprint(report, failure)
            fingerprints.append(fingerprint)
            connection.execute(
                """INSERT INTO ralph_failures (
                    fingerprint, run_id, url, domain, stage, summary, evidence_json,
                    occurrences, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    run_id=excluded.run_id,
                    occurrences=ralph_failures.occurrences + 1,
                    evidence_json=excluded.evidence_json,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    fingerprint, run_id, report.url, report.domain, failure.stage,
                    failure.summary, json.dumps(failure.evidence, ensure_ascii=False), now, now,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return run_id, fingerprints


def render_issue(report: RatingReport, run_id: str, failure: StageRating) -> dict[str, object]:
    fingerprint = failure_fingerprint(report, failure)
    title = f"[Ralph][{failure.stage}] {report.domain}: {failure.summary}"
    evidence = json.dumps(failure.evidence, ensure_ascii=False, indent=2, sort_keys=True)
    body = f"""## Automated failure

- **Run:** `{run_id}`
- **Source:** `{report.domain}`
- **Job:** [{report.title}]({report.url})
- **Company:** {report.company}
- **Rating:** {report.score}/100
- **Stage:** `{failure.stage}`
- **Observed:** {failure.summary}
- **Fingerprint:** `{fingerprint}`

## Evidence

```json
{evidence}
```

## Reproduction

```bash
python -m ralph.first_loop --url "{report.url}"
```

## Acceptance criteria

- [ ] Reproduce the failure with a sanitized fixture.
- [ ] Add or improve the source-specific parser/application adapter.
- [ ] Add a regression test for this URL shape.
- [ ] Run `python -m unittest discover -s tests -v`.
- [ ] Rerun this Ralph rating and make the failing stage pass.

_Created deterministically by the Ralph rating chain; no LLM was used to rate or write this issue._
"""
    return {"title": title[:250], "body": body, "fingerprint": fingerprint, "run_id": run_id}


def mark_issue_created(db_path: Path, fingerprint: str, issue_number: int) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE ralph_failures SET issue_number=?, status='issue-open' WHERE fingerprint=?",
            (issue_number, fingerprint),
        )
        connection.commit()
    finally:
        connection.close()

