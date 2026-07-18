"""SQLite persistence for Ralph checkpoints and transcript-free findings."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import ReviewReport

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ralph_review_checkpoints (
    peer_key TEXT PRIMARY KEY,
    last_message_id INTEGER NOT NULL,
    marker_message_id INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ralph_review_runs (
    id TEXT PRIMARY KEY,
    peer_key TEXT NOT NULL,
    marker_message_id INTEGER,
    marker_run_id TEXT,
    start_message_id INTEGER NOT NULL,
    end_message_id INTEGER NOT NULL,
    message_count INTEGER NOT NULL,
    finding_count INTEGER NOT NULL,
    report_path TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ralph_review_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    interaction_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    message_ids_json TEXT NOT NULL,
    timestamps_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Checkpoint:
    peer_key: str
    last_message_id: int
    marker_message_id: int | None


class RalphStore:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.executescript(_SCHEMA)
        return connection

    def get_checkpoint(self, peer_key: str) -> Checkpoint | None:
        if not self.path.is_file():
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT peer_key, last_message_id, marker_message_id "
                "FROM ralph_review_checkpoints WHERE peer_key=?",
                (peer_key,),
            ).fetchone()
            return Checkpoint(*row) if row else None
        finally:
            connection.close()

    def save_review(self, report: ReviewReport, report_path: Path) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            connection.execute(
                """INSERT INTO ralph_review_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report.id, report.peer_key, report.marker_message_id,
                    report.marker_run_id, report.start_message_id,
                    report.end_message_id, report.analyzed_messages,
                    len(report.findings), str(report_path), "completed", report.created_at,
                ),
            )
            for finding in report.findings:
                connection.execute(
                    """INSERT INTO ralph_review_findings (
                        run_id, rule_id, severity, interaction_id, summary,
                        message_ids_json, timestamps_json, evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        report.id, finding.rule_id, finding.severity,
                        finding.interaction_id, finding.summary,
                        json.dumps(finding.message_ids),
                        json.dumps(finding.timestamps),
                        json.dumps(finding.evidence, ensure_ascii=False, sort_keys=True),
                    ),
                )
            connection.execute(
                """INSERT INTO ralph_review_checkpoints (
                    peer_key, last_message_id, marker_message_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(peer_key) DO UPDATE SET
                    last_message_id=excluded.last_message_id,
                    marker_message_id=excluded.marker_message_id,
                    updated_at=excluded.updated_at""",
                (
                    report.peer_key, report.end_message_id,
                    report.marker_message_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def write_report(report: ReviewReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
