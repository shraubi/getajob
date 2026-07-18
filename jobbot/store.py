"""SQLite persistence for fetched jobs and submission state."""

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobbot.integrations.job_page import ParsedJobPage
from jobbot.application import Vacancy

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
    recruiter_message TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'parsed',
    idempotency_key TEXT NOT NULL UNIQUE,
    external_message_id TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    sent_at TEXT
)
"""

_OUTBOUND_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    error_type TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    attempted_at TEXT NOT NULL
)
"""

_COOLDOWN_SCHEMA = """
CREATE TABLE IF NOT EXISTS sender_cooldowns (
    channel TEXT PRIMARY KEY,
    blocked_until TEXT NOT NULL,
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_SCHEMA)
    connection.execute(_OUTBOUND_SCHEMA)
    connection.execute(_COOLDOWN_SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    if "recruiter_message" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN recruiter_message TEXT NOT NULL DEFAULT ''")


def save_fetched_job(
    db_path: Path,
    page: ParsedJobPage,
    direction: str,
    resume_name: str,
    recruiter_message: str = "",
) -> str:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    job_id = hashlib.sha256(page.fetched_url.encode("utf-8")).hexdigest()
    send_key = hashlib.sha256((job_id + ":" + page.contact_value).encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(db_path)
    try:
        _ensure_schema(connection)
        connection.execute(
            """INSERT INTO jobs (
                id, page_url, title, company, description, source_category,
                apply_url, contact_kind, contact_value, direction, resume_name,
                recruiter_message, idempotency_key, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, company=excluded.company,
                description=excluded.description, source_category=excluded.source_category,
                apply_url=excluded.apply_url, contact_kind=excluded.contact_kind,
                contact_value=excluded.contact_value, direction=excluded.direction,
                resume_name=excluded.resume_name, recruiter_message=excluded.recruiter_message,
                idempotency_key=excluded.idempotency_key,
                last_seen_at=excluded.last_seen_at
            """,
            (
                job_id, page.fetched_url, page.vacancy.title, page.vacancy.company,
                page.vacancy.description, page.source_category, page.apply_url,
                page.contact_kind, page.contact_value, direction, resume_name, recruiter_message,
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


def get_job_by_prefix(db_path: Path, job_id_prefix: str) -> dict | None:
    if not db_path.is_file() or not re.fullmatch(r"[0-9a-f]{12,32}", job_id_prefix):
        return None
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM jobs WHERE id LIKE ? LIMIT 2", (job_id_prefix + "%",)).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None
    finally:
        connection.close()


def claim_job_for_send(db_path: Path, job_id: str) -> bool:
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.execute(
            "UPDATE jobs SET status='sending' WHERE id=? AND status IN ('parsed', 'send_failed')",
            (job_id,),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def claim_telegram_job_for_send(
    db_path: Path,
    job_id: str,
    *,
    min_interval_seconds: int,
    max_per_hour: int,
) -> tuple[bool, datetime | None, str]:
    """Atomically reserve the single Telegram sender while enforcing limits."""
    now = datetime.now(timezone.utc)
    connection = sqlite3.connect(db_path, timeout=10)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        target = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not target or target[0] not in {"parsed", "send_failed"}:
            connection.rollback()
            return False, None, ""

        cooldown = connection.execute(
            "SELECT blocked_until, reason FROM sender_cooldowns WHERE channel='telegram'"
        ).fetchone()
        if cooldown:
            blocked_until = datetime.fromisoformat(cooldown[0])
            if blocked_until > now:
                connection.rollback()
                return False, blocked_until, cooldown[1]

        active = connection.execute(
            "SELECT 1 FROM jobs WHERE contact_kind='telegram' AND status='sending' AND id<>? LIMIT 1",
            (job_id,),
        ).fetchone()
        if active:
            connection.rollback()
            return False, now + timedelta(seconds=60), "another Telegram application is sending"

        latest = connection.execute(
            """SELECT sent_at FROM jobs
               WHERE contact_kind='telegram' AND status='sent' AND sent_at IS NOT NULL
               ORDER BY sent_at DESC LIMIT 1"""
        ).fetchone()
        if latest and min_interval_seconds > 0:
            retry_at = datetime.fromisoformat(latest[0]) + timedelta(seconds=min_interval_seconds)
            if retry_at > now:
                connection.rollback()
                return False, retry_at, "minimum interval between Telegram applications"

        cutoff = (now - timedelta(hours=1)).isoformat()
        recent_count = connection.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE contact_kind='telegram' AND status='sent' AND sent_at >= ?""",
            (cutoff,),
        ).fetchone()[0]
        if max_per_hour > 0 and recent_count >= max_per_hour:
            oldest = connection.execute(
                """SELECT sent_at FROM jobs
                   WHERE contact_kind='telegram' AND status='sent' AND sent_at >= ?
                   ORDER BY sent_at ASC LIMIT 1""",
                (cutoff,),
            ).fetchone()[0]
            retry_at = datetime.fromisoformat(oldest) + timedelta(hours=1)
            connection.rollback()
            return False, retry_at, f"hourly Telegram limit ({max_per_hour})"

        cursor = connection.execute(
            "UPDATE jobs SET status='sending' WHERE id=? AND status IN ('parsed', 'send_failed')",
            (job_id,),
        )
        connection.commit()
        return cursor.rowcount == 1, None, ""
    finally:
        connection.close()


def set_sender_cooldown(db_path: Path, channel: str, blocked_until: datetime, reason: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        _ensure_schema(connection)
        connection.execute(
            """INSERT INTO sender_cooldowns (channel, blocked_until, reason, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(channel) DO UPDATE SET
                   blocked_until=excluded.blocked_until,
                   reason=excluded.reason,
                   updated_at=excluded.updated_at""",
            (channel, blocked_until.isoformat(), reason, datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
    finally:
        connection.close()


def record_send_attempt(
    db_path: Path,
    job_id: str,
    channel: str,
    target: str,
    status: str,
    error: Exception | None = None,
) -> None:
    connection = sqlite3.connect(db_path)
    try:
        _ensure_schema(connection)
        connection.execute(
            """INSERT INTO outbound_attempts (
                   job_id, channel, target, status, error_type, error_message, attempted_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id, channel, target, status,
                type(error).__name__ if error else "", str(error)[:1000] if error else "",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def mark_job_sent(db_path: Path, job_id: str, external_message_id: int) -> bool:
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.execute(
            """UPDATE jobs SET status='sent', external_message_id=?, sent_at=?
               WHERE id=? AND status='sending'""",
            (str(external_message_id), datetime.now(timezone.utc).isoformat(), job_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def mark_job_send_failed(db_path: Path, job_id: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE jobs SET status='send_failed' WHERE id=? AND status='sending'", (job_id,))
        connection.commit()
    finally:
        connection.close()
