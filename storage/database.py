"""Small durable SQLite store for replayable parsed jobs and idempotent actions."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from bot.parser import JobSource, Vacancy


class JobStore:
    def __init__(self, path: str | Path = "data/getajob.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY, source_url TEXT NOT NULL UNIQUE,
              source TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actions (
              idempotency_key TEXT PRIMARY KEY, job_id INTEGER NOT NULL,
              status TEXT NOT NULL, evidence TEXT, created_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES jobs(id)
            );
        """)

    def save_job(self, vacancy: Vacancy) -> int:
        payload = asdict(vacancy)
        payload["source"] = vacancy.source.value
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            "INSERT INTO jobs(source_url, source, payload, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(source_url) DO UPDATE SET source=excluded.source, payload=excluded.payload",
            (vacancy.source_url, vacancy.source.value, json.dumps(payload, ensure_ascii=False), now),
        )
        self.connection.commit()
        row = self.connection.execute("SELECT id FROM jobs WHERE source_url=?", (vacancy.source_url,)).fetchone()
        return int(row["id"])

    def begin_action(self, key: str, job_id: int) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO actions(idempotency_key, job_id, status, created_at) VALUES(?,?,?,?)",
            (key, job_id, "pending", datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def get_job(self, job_id: int) -> Vacancy | None:
        row = self.connection.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["source"] = JobSource(payload["source"])
        return Vacancy(**payload)

    def finish_action(self, key: str, evidence: str) -> None:
        self.connection.execute(
            "UPDATE actions SET status='sent', evidence=? WHERE idempotency_key=?", (evidence, key)
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
