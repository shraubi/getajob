"""SQLite persistence for parsed jobs."""

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from token_free import Vacancy

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source_category TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    salary TEXT NOT NULL,
    location TEXT NOT NULL,
    work_format TEXT NOT NULL,
    employment TEXT NOT NULL,
    seniority TEXT NOT NULL,
    language TEXT NOT NULL,
    skills_json TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    direction TEXT NOT NULL,
    resume_name TEXT NOT NULL,
    status TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
)
"""


def _job_id(vacancy: Vacancy) -> str:
    identity = vacancy.url or "\n".join((vacancy.source_category, vacancy.title, vacancy.description))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def save_job(db_path: Path, vacancy: Vacancy, direction: str, resume_name: str) -> str:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    job_id = _job_id(vacancy)
    now = datetime.now(timezone.utc).isoformat()
    values = asdict(vacancy)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_SCHEMA)
        connection.execute(
            """INSERT INTO jobs (
                id, source_category, source_url, title, company, salary, location,
                work_format, employment, seniority, language, skills_json, raw_text,
                direction, resume_name, status, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'parsed', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                source_category=excluded.source_category, source_url=excluded.source_url,
                title=excluded.title, company=excluded.company, salary=excluded.salary,
                location=excluded.location, work_format=excluded.work_format,
                employment=excluded.employment, seniority=excluded.seniority,
                language=excluded.language, skills_json=excluded.skills_json,
                raw_text=excluded.raw_text, direction=excluded.direction,
                resume_name=excluded.resume_name, last_seen_at=excluded.last_seen_at
            """,
            (
                job_id, values["source_category"], values["url"], values["title"],
                values["company"], values["salary"], values["location"],
                values["work_format"], values["employment"], values["seniority"],
                values["language"], json.dumps(values["skills"], ensure_ascii=False),
                values["description"], direction, resume_name, now, now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return job_id


def get_job(db_path: Path, job_id: str) -> dict | None:
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        connection.close()
    return dict(row) if row else None
