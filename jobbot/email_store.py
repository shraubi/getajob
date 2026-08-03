"""Durable, content-free state for inbound job-alert emails."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_email_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox_key TEXT NOT NULL,
    uid_validity TEXT NOT NULL,
    uid TEXT NOT NULL,
    message_id_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    parser_revision INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    handled_at TEXT,
    UNIQUE(mailbox_key, uid_validity, uid)
);
CREATE TABLE IF NOT EXISTS inbound_offers (
    provider TEXT NOT NULL,
    offer_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    source_message_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, offer_id)
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    columns = {
        row[1] for row in connection.execute(
            "PRAGMA table_info(inbound_email_messages)"
        )
    }
    if "parser_revision" not in columns:
        connection.execute(
            """ALTER TABLE inbound_email_messages
               ADD COLUMN parser_revision INTEGER NOT NULL DEFAULT 1"""
        )
    return connection


def _digest(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8", errors="replace")
    return hashlib.sha256(value).hexdigest()


def record_email_offers(
    db_path: Path,
    *,
    mailbox_key: str,
    uid_validity: str,
    uid: str,
    message_id: str,
    raw_message: bytes,
    offers: tuple[tuple[str, str], ...],
    parser_revision: int = 1,
) -> tuple[int, int, bool]:
    """Persist one receipt and its offers atomically.

    Returns ``(inserted_offers, duplicate_offers, already_recorded)``.
    """
    now = datetime.now(timezone.utc).isoformat()
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """SELECT id, status FROM inbound_email_messages
               WHERE (mailbox_key=? AND uid_validity=? AND uid=?) OR content_hash=?""",
            (mailbox_key, uid_validity, uid, _digest(raw_message)),
        ).fetchone()
        if existing and existing["status"] != "rejected":
            connection.rollback()
            return 0, len(offers), True
        if existing:
            receipt_id = int(existing["id"])
        else:
            cursor = connection.execute(
                """INSERT INTO inbound_email_messages (
                       mailbox_key, uid_validity, uid, message_id_hash, content_hash,
                       status, parser_revision, first_seen_at
                   ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (
                    mailbox_key, uid_validity, uid, _digest(message_id),
                    _digest(raw_message), parser_revision, now,
                ),
            )
            receipt_id = int(cursor.lastrowid)
        inserted = 0
        for offer_id, canonical_url in offers:
            result = connection.execute(
                """INSERT OR IGNORE INTO inbound_offers (
                       provider, offer_id, canonical_url, source_message_id,
                       created_at, updated_at
                   ) VALUES ('hellowork', ?, ?, ?, ?, ?)""",
                (offer_id, canonical_url, receipt_id, now, now),
            )
            inserted += result.rowcount
        duplicates = len(offers) - inserted
        connection.execute(
            """UPDATE inbound_email_messages
               SET status='queued', discovered_count=?, duplicate_count=?,
                   last_error='', parser_revision=?, handled_at=? WHERE id=?""",
            (len(offers), duplicates, parser_revision, now, receipt_id),
        )
        connection.commit()
        return inserted, duplicates, False
    finally:
        connection.close()


def record_rejected_email(
    db_path: Path,
    *,
    mailbox_key: str,
    uid_validity: str,
    uid: str,
    message_id: str,
    raw_message: bytes,
    reason: str,
    parser_revision: int = 1,
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    connection = _connect(db_path)
    try:
        existing = connection.execute(
            """SELECT id, status FROM inbound_email_messages
               WHERE (mailbox_key=? AND uid_validity=? AND uid=?) OR content_hash=?""",
            (mailbox_key, uid_validity, uid, _digest(raw_message)),
        ).fetchone()
        if existing:
            if existing["status"] != "rejected":
                return False
            connection.execute(
                """UPDATE inbound_email_messages
                   SET last_error=?, parser_revision=?, handled_at=? WHERE id=?""",
                (reason[:500], parser_revision, now, existing["id"]),
            )
            connection.commit()
            return True
        cursor = connection.execute(
            """INSERT INTO inbound_email_messages (
                   mailbox_key, uid_validity, uid, message_id_hash, content_hash,
                   status, last_error, parser_revision, first_seen_at, handled_at
               ) VALUES (?, ?, ?, ?, ?, 'rejected', ?, ?, ?, ?)""",
            (
                mailbox_key, uid_validity, uid, _digest(message_id),
                _digest(raw_message), reason[:500], parser_revision, now, now,
            ),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def replayable_rejected_uids(
    db_path: Path,
    *,
    mailbox_key: str,
    uid_validity: str,
    parser_revision: int,
    limit: int = 25,
) -> tuple[str, ...]:
    if not db_path.is_file():
        return ()
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            """SELECT uid FROM inbound_email_messages
               WHERE mailbox_key=? AND uid_validity=? AND status='rejected'
                 AND parser_revision < ?
                 AND last_error IN (
                     'no HelloWork tracking links found',
                     'no valid HelloWork offer links found'
                 )
               ORDER BY id LIMIT ?""",
            (mailbox_key, uid_validity, parser_revision, max(1, min(limit, 25))),
        ).fetchall()
        return tuple(str(row["uid"]) for row in rows)
    finally:
        connection.close()


def mark_replay_unavailable(
    db_path: Path,
    *,
    mailbox_key: str,
    uid_validity: str,
    uids: tuple[str, ...],
    parser_revision: int,
) -> int:
    if not uids or not db_path.is_file():
        return 0
    now = datetime.now(timezone.utc).isoformat()
    connection = _connect(db_path)
    try:
        updated = 0
        for uid in uids:
            cursor = connection.execute(
                """UPDATE inbound_email_messages
                   SET last_error='replay unavailable: message not found',
                       parser_revision=?, handled_at=?
                   WHERE mailbox_key=? AND uid_validity=? AND uid=?
                     AND status='rejected' AND parser_revision < ?""",
                (
                    parser_revision, now, mailbox_key, uid_validity, uid,
                    parser_revision,
                ),
            )
            updated += cursor.rowcount
        connection.commit()
        return updated
    finally:
        connection.close()


def close_stale_rejected_emails(
    db_path: Path,
    *,
    mailbox_key: str,
    current_uid_validity: str,
    parser_revision: int,
) -> int:
    if not db_path.is_file():
        return 0
    now = datetime.now(timezone.utc).isoformat()
    connection = _connect(db_path)
    try:
        cursor = connection.execute(
            """UPDATE inbound_email_messages
               SET last_error='replay unavailable: UIDVALIDITY changed',
                   parser_revision=?, handled_at=?
               WHERE mailbox_key=? AND uid_validity<>? AND status='rejected'
                 AND parser_revision < ?
                 AND last_error IN (
                     'no HelloWork tracking links found',
                     'no valid HelloWork offer links found'
                 )""",
            (
                parser_revision, now, mailbox_key, current_uid_validity,
                parser_revision,
            ),
        )
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def claim_next_offer(db_path: Path) -> dict | None:
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        connection.execute(
            """UPDATE inbound_offers SET status='pending', updated_at=?
               WHERE status='processing' AND updated_at < ?""",
            (datetime.now(timezone.utc).isoformat(), stale),
        )
        row = connection.execute(
            """SELECT * FROM inbound_offers WHERE status='pending'
               ORDER BY created_at LIMIT 1"""
        ).fetchone()
        if not row:
            connection.rollback()
            return None
        connection.execute(
            """UPDATE inbound_offers
               SET status='processing', attempts=attempts+1, updated_at=?
               WHERE provider=? AND offer_id=?""",
            (datetime.now(timezone.utc).isoformat(), row["provider"], row["offer_id"]),
        )
        connection.commit()
        return dict(row)
    finally:
        connection.close()


def finish_offer(db_path: Path, offer_id: str, status: str, error: str = "") -> None:
    if status not in {"completed", "paused", "failed", "skipped"}:
        raise ValueError("invalid inbound-offer status")
    connection = _connect(db_path)
    try:
        connection.execute(
            """UPDATE inbound_offers SET status=?, last_error=?, updated_at=?
               WHERE provider='hellowork' AND offer_id=?""",
            (status, error[:500], datetime.now(timezone.utc).isoformat(), offer_id),
        )
        connection.commit()
    finally:
        connection.close()


def get_offer(db_path: Path, offer_id: str) -> dict | None:
    if not db_path.is_file():
        return None
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM inbound_offers WHERE provider='hellowork' AND offer_id=?",
            (offer_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()

