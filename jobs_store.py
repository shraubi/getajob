"""SQLite persistence for fetched jobs and submission state."""

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from job_page import ParsedJobPage
from token_free import Vacancy

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    page_url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    description TEXT NOT NULL,
    source_category TEXT NOT NULL,
    apply_url TEXT NOT NULL,
    contact_kind TEXT NOT NULL,
    contact_value TEXT NOT NULL,
    direction TEXT NOT NULL,
    resume_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'parsed',
    idempotency_key TEXT NOT NULL UNIQUE,
    external_message_id TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    sent_at TEXT
)
"""


def save_fetched_job(db_path: Path, page: ParsedJobPage, direction: str, resume_name: str) -> str:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    job_id = hashlib.sha256(page.fetched_url.encode("utf-8")).hexdigest()
    send_key = hashlib.sha256((job_id + ":" + page.contact_value).encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_SCHEMA)
        connection.execute(
            """INSERT INTO jobs (
                id, page_url, title, company, description, source_category,
                apply_url, contact_kind, contact_value, direction, resume_name,
                idempotency_key, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, company=excluded.company,
                description=excluded.description, source_category=excluded.source_category,
                apply_url=excluded.apply_url, contact_kind=excluded.contact_kind,
                contact_value=excluded.contact_value, direction=excluded.direction,
                resume_name=excluded.resume_name, idempotency_key=excluded.idempotency_key,
                last_seen_at=excluded.last_seen_at
            """,
            (
                job_id, page.fetched_url, page.vacancy.title, page.vacancy.company,
                page.vacancy.description, page.source_category, page.apply_url,
                page.contact_kind, page.contact_value, direction, resume_name,
                send_key, now, now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return job_id


def save_job(db_path: Path, vacancy: Vacancy, direction: str, resume_name: str) -> str:
    """Backward-compatible persistence for messages without a fetched page."""
    identity = vacancy.url or "telegram:" + hashlib.sha256(
        (vacancy.title + "\n" + vacancy.description).encode("utf-8")
    ).hexdigest()
    page = ParsedJobPage(
        vacancy=vacancy,
        source_category=vacancy.source_category,
        apply_url=vacancy.url,
        fetched_url=identity,
    )
    return save_fetched_job(db_path, page, direction, resume_name)


def get_job(db_path: Path, job_id: str) -> dict | None:
    if not db_path.is_file():
        return None
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()
