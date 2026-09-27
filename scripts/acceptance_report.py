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


def _failure_category(detail: str) -> str:
    """Map private failure text to a fixed, non-identifying reason code."""
    value = detail.casefold()
    if "executable doesn\u0027t exist" in value or "browser is missing" in value:
        return "browser_missing"
    if "browsertype.launch" in value:
        return "browser_launch"
    if "timeouterror" in value or "timed out" in value:
        return "page_timeout"
    if "net::err_" in value:
        return "network_error"
    if "captcha" in value:
        return "captcha"
    if "auth_required" in value or "login or verification" in value:
        return "authentication_required"
    if "confirmation_required" in value:
        return "confirmation_required"
    if "submission_unknown" in value:
        return "submission_unknown"
    if "strict mode violation" in value:
        return "ambiguous_form_control"
    if "unavailable" in value:
        return "offer_unavailable"
    return "other_failure"


def assess(db_path: Path, *, since: datetime, now: datetime, stale_minutes: int = 30) -> dict:
    """Return a safe summary; never expose messages, URLs, or stored error text."""
    report = {
        "status": "FAIL",
        "since": since.isoformat(),
        "checked_at": now.isoformat(),
        "email_statuses": {},
        "offer_statuses": {},
        "failure_categories": {},
        "stale_offer_count": 0,
        "attention_offer_count": 0,
        "rechecked_offer_count": 0,
        "rechecked_new_submission_count": 0,
        "unverified_completed_count": 0,
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
                """SELECT status, last_error, created_at, updated_at
                   FROM inbound_offers WHERE provider='hellowork'
                     AND (created_at >= ? OR updated_at >= ?)""",
                (since.isoformat(), since.isoformat()),
            ).fetchall()
            active_rows = connection.execute(
                """SELECT status, updated_at FROM inbound_offers
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
    report["failure_categories"] = dict(sorted(Counter(
        _failure_category(str(row["last_error"] or ""))
        for row in offer_rows if row["status"] in {"failed", "paused"}
    ).items()))
    cutoff = now - timedelta(minutes=stale_minutes)
    stale_count = sum(
        _utc(str(row["updated_at"])) < cutoff for row in active_rows
    )
    attention_count = sum(
        row["status"] in {"failed", "paused"} for row in offer_rows
    )
    rechecked_count = sum(
        row["status"] == "completed"
        and str(row["last_error"] or "").startswith("account_marker_recheck=1")
        for row in offer_rows
    )
    report["stale_offer_count"] = stale_count
    report["attention_offer_count"] = attention_count
    report["rechecked_offer_count"] = rechecked_count
    report["rechecked_new_submission_count"] = sum(
        row["status"] == "completed"
        and str(row["last_error"] or "").startswith("account_marker_recheck=1")
        and "completed_steps=" in str(row["last_error"] or "")
        for row in offer_rows
    )
    report["unverified_completed_count"] = offers.get("completed", 0) - rechecked_count
    if stale_count:
        report["reasons"].append("offers_stuck_in_queue")
    if emails.get("rejected", 0):
        report["reasons"].append("emails_rejected")
    if offers.get("failed", 0):
        report["reasons"].append("applications_failed")
    if offers.get("paused", 0):
        report["reasons"].append("applications_need_attention")
    if any(status not in {"completed", "pending", "processing", "failed", "paused"}
           for status in offers):
        report["reasons"].append("offers_not_applied")
    if report["reasons"]:
        return report
    if not email_rows and not offer_rows:
        report["status"] = "WAITING_FOR_LIVE_TRAFFIC"
    elif active_rows:
        report["status"] = "WAITING_FOR_TERMINAL_OUTCOME"
    elif offers.get("completed", 0):
        if report["unverified_completed_count"]:
            report["status"] = "UNVERIFIED_COMPLETION"
            report["reasons"].append("account_marker_not_rechecked")
        elif not report["rechecked_new_submission_count"]:
            report["status"] = "NO_NEW_SUBMISSION"
            report["reasons"].append("only_previously_applied_offers")
        else:
            report["status"] = "RECHECKED_APPLIED"
    else:
        report["status"] = "WAITING_FOR_TERMINAL_OUTCOME"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--since", help="ISO-8601 timestamp; defaults to 24 hours ago")
    parser.add_argument("--stale-minutes", type=int, default=30)
    parser.add_argument(
        "--allow-waiting", action="store_true",
        help="Allow deploy to pass before live application traffic arrives",
    )
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    since = _utc(args.since) if args.since else now - timedelta(hours=24)
    report = assess(
        args.db, since=since, now=now, stale_minutes=max(1, args.stale_minutes)
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    passing = report["status"] == "RECHECKED_APPLIED" or (
        args.allow_waiting and (
            report["status"].startswith("WAITING_")
            or report["status"] == "NO_NEW_SUBMISSION"
        )
    )
    return 0 if passing else 1


if __name__ == "__main__":
    raise SystemExit(main())
