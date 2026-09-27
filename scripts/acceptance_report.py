"""Read-only, content-free production assessment for GitHub Actions."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def assess(db_path: Path, *, since: datetime, now: datetime, stale_minutes: int = 30) -> dict:
    """Return a safe summary; never expose messages, URLs, or stored error text."""
    report = {
        "status": "FAIL",
        "since": since.isoformat(),
        "checked_at": now.isoformat(),
        "email_statuses": {},
        "offer_statuses": {},
        "stale_offer_count": 0,
        "attention_offer_count": 0,
        "reasons": [],
    }
    if not db_path.is_file():
        report["reasons"].append("jobs_database_missing")
        return report

    try:
        connection = sqlite3.connect(
            f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=10
        )
        connection.row_factory = sqlite3.Row
        try:
            email_rows = connection.execute(
                """SELECT status FROM inbound_email_messages
                   WHERE first_seen_at >= ? OR handled_at >= ?""",
                (since.isoformat(), since.isoformat()),
            ).fetchall()
            offer_rows = connection.execute(
                """SELECT offer_id, status, created_at, updated_at
                   FROM inbound_offers WHERE provider='hellowork'
                     AND (created_at >= ? OR updated_at >= ?)""",
                (since.isoformat(), since.isoformat()),
            ).fetchall()
            active_rows = connection.execute(
                """SELECT offer_id, status, updated_at FROM inbound_offers
                   WHERE provider='hellowork' AND status IN ('pending', 'processing')"""
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        report["reasons"].append("jobs_database_schema_unavailable")
        return report

    emails = Counter(str(row["status"]) for row in email_rows)
    offers = Counter(str(row["status"]) for row in offer_rows)
    report["email_statuses"] = dict(sorted(emails.items()))
    report["offer_statuses"] = dict(sorted(offers.items()))
    cutoff = now - timedelta(minutes=stale_minutes)
    stale = [
        str(row["offer_id"]) for row in active_rows
        if _utc(str(row["updated_at"])) < cutoff
    ]
    attention = [
        str(row["offer_id"]) for row in offer_rows
        if row["status"] in {"failed", "paused"}
    ]
    report["stale_offer_count"] = len(stale)
    report["attention_offer_count"] = len(attention)
    if stale:
        report["reasons"].append("offers_stuck_in_queue")
    if emails.get("rejected", 0):
        report["reasons"].append("emails_rejected")
    if offers.get("failed", 0):
        report["reasons"].append("applications_failed")
    if offers.get("paused", 0):
        report["reasons"].append("applications_need_attention")
    if report["reasons"]:
        return report
    if not email_rows and not offer_rows:
        report["status"] = "WAITING_FOR_LIVE_TRAFFIC"
    elif offers.get("completed", 0):
        report["status"] = "RECORDED_COMPLETION"
    else:
        report["status"] = "WAITING_FOR_TERMINAL_OUTCOME"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--since", help="ISO-8601 timestamp; defaults to 24 hours ago")
    parser.add_argument("--stale-minutes", type=int, default=30)
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    since = _utc(args.since) if args.since else now - timedelta(hours=24)
    report = assess(
        args.db, since=since, now=now, stale_minutes=max(1, args.stale_minutes)
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
