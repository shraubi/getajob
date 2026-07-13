"""Run one deterministic rating chain and emit its first issue payload."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .rating import rate_job
from .store import record_report, render_issue
from .telegram_flow import review_bot_output, send_job_to_bot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--expected-direction")
    parser.add_argument("--direct", action="store_true", help="Rate code directly instead of exercising Telegram")
    parser.add_argument("--db", type=Path, default=Path("storage/ralph.db"))
    parser.add_argument("--issue-output", type=Path, default=Path("storage/ralph_issue.json"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()
    if args.direct:
        report = asyncio.run(rate_job(args.url, expected_direction=args.expected_direction))
        telegram_run_id = ""
    else:
        api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
        api_hash = os.environ.get("TELEGRAM_API_HASH", "")
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        session_path = Path(os.environ.get("RALPH_TELEGRAM_SESSION_PATH") or os.environ.get("TELEGRAM_SESSION_PATH", "storage/telegram_sender"))
        if not api_id or not api_hash or not bot_token:
            raise SystemExit("Telegram first loop requires TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN")
        telegram_run_id, messages = asyncio.run(send_job_to_bot(
            args.url,
            api_id=api_id,
            api_hash=api_hash,
            session_path=session_path,
            bot_token=bot_token,
        ))
        report = review_bot_output(args.url, messages, expected_direction=args.expected_direction)
    run_id, _ = record_report(args.db, report)
    issue = render_issue(report, run_id, report.failures[0]) if report.failures else None
    payload = {"run_id": run_id, "telegram_run_id": telegram_run_id, "report": report.to_dict(), "issue": issue}
    if issue:
        args.issue_output.parent.mkdir(parents=True, exist_ok=True)
        args.issue_output.write_text(json.dumps(issue, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

