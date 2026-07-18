"""Manual CLI for read-only review of Jobbot Telegram history."""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from jobbot import config

from .analyzer import analyze_interactions, extract_urls, group_interactions
from .history import (
    RalphHistoryError,
    fetch_telegram_history,
    parse_since,
    resolve_bot_username,
)
from .models import ReviewReport
from .store import RalphStore, write_report

_DB_PATH = Path("storage/ralph.db")
_REPORT_DIR = Path("storage/ralph/reviews")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review recent Jobbot Telegram history")
    parser.add_argument("--since", help="ISO-8601 fallback when no Ralph marker exists")
    parser.add_argument(
        "--replay-latest-run", action="store_true",
        help="Ignore the saved checkpoint and review from the latest marker",
    )
    parser.add_argument("--output", type=Path, help="Structured JSON report path")
    return parser.parse_args(argv)


async def run_review(args: argparse.Namespace) -> tuple[ReviewReport, Path]:
    since = parse_since(args.since)
    bot_username = await resolve_bot_username(config.TELEGRAM_BOT_TOKEN)
    peer_key = bot_username.casefold()
    store = RalphStore(_DB_PATH)
    checkpoint = store.get_checkpoint(peer_key)
    history = await fetch_telegram_history(
        api_id=config.TELEGRAM_API_ID,
        api_hash=config.TELEGRAM_API_HASH,
        session_path=config.TELEGRAM_SESSION_PATH,
        bot_username=bot_username,
        checkpoint_message_id=(checkpoint.last_message_id if checkpoint else None),
        since=since,
        replay_latest_run=args.replay_latest_run,
    )
    interactions = group_interactions(
        history.messages, seed_request=history.seed_request
    )
    findings = analyze_interactions(interactions)
    review_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    end_message_id = (
        history.messages[-1].id if history.messages else history.boundary_message_id
    )
    source_messages = (
        ((history.seed_request,) if history.seed_request else ())
        + history.messages
    )
    report = ReviewReport(
        id=review_id,
        peer_key=history.peer_key,
        marker_message_id=(history.marker.message.id if history.marker else None),
        marker_run_id=(history.marker.run_id if history.marker else None),
        start_message_id=history.boundary_message_id,
        end_message_id=end_message_id,
        analyzed_messages=len(history.messages),
        source_urls=extract_urls(source_messages),
        has_more=history.has_more,
        findings=findings,
        created_at=now,
    )
    output = args.output or (_REPORT_DIR / f"{review_id}.json")
    write_report(report, output)
    store.save_review(report, output)
    return report, output


def _print_summary(report: ReviewReport, output: Path) -> None:
    print(
        f"Ralph reviewed {report.analyzed_messages} messages and found "
        f"{len(report.findings)} issue(s)."
    )
    for finding in report.findings:
        print(f"- [{finding.severity}] {finding.rule_id}: {finding.summary}")
    for url in report.source_urls:
        print(f"  {url}")
    if report.has_more:
        print("More messages remain; run the same command again for the next chunk.")
    print(f"Report: {output}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report, output = asyncio.run(run_review(args))
    except RalphHistoryError as exc:
        print(f"Ralph review failed: {exc}", file=sys.stderr)
        return 2
    _print_summary(report, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
