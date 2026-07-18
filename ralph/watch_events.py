"""Continuously review Jobbot's structured cloud event journal."""
from __future__ import annotations
import argparse
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .event_review import analyze_events, event_urls, read_event_batch
from .models import ReviewReport
from .store import RalphStore, write_report

_SOURCE_DB = Path(os.environ.get("RALPH_SOURCE_DB_PATH", "storage/jobs.db"))
_RALPH_DB = Path(os.environ.get("RALPH_DB_PATH", "storage/ralph.db"))
_REPORT_DIR = Path(os.environ.get("RALPH_REPORT_DIR", "storage/ralph/reviews"))
_PEER_KEY = "jobbot-events"

def review_once() -> tuple[ReviewReport, Path] | None:
    store = RalphStore(_RALPH_DB)
    checkpoint = store.get_checkpoint(_PEER_KEY)
    after_id = checkpoint.last_message_id if checkpoint else 0
    events, has_more = read_event_batch(_SOURCE_DB, after_id=after_id)
    if not events:
        return None
    review_id = uuid.uuid4().hex
    report = ReviewReport(
        id=review_id, peer_key=_PEER_KEY, marker_message_id=None, marker_run_id=None,
        start_message_id=after_id, end_message_id=events[-1].id,
        analyzed_messages=len(events), source_urls=event_urls(events), has_more=has_more,
        findings=analyze_events(events), created_at=datetime.now(timezone.utc).isoformat(),
    )
    output = _REPORT_DIR / f"events-{review_id}.json"
    write_report(report, output)
    store.save_review(report, output)
    return report, output

def _print(report: ReviewReport, output: Path) -> None:
    print(f"Ralph reviewed {report.analyzed_messages} Jobbot event(s) and found "
          f"{len(report.findings)} issue(s).", flush=True)
    for finding in report.findings:
        print(f"- [{finding.severity}] {finding.rule_id}: {finding.summary}", flush=True)
        for url in finding.evidence.get("urls", ()):
            print(f"  {url}", flush=True)
    print(f"Report: {output}", flush=True)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review Jobbot events continuously")
    parser.add_argument("--once", action="store_true", help="Review one available batch and exit")
    parser.add_argument("--poll-seconds", type=float,
                        default=float(os.environ.get("RALPH_POLL_SECONDS", "5")))
    args = parser.parse_args(argv)
    while True:
        result = review_once()
        if result:
            _print(*result)
            if result[0].has_more:
                continue
        if args.once:
            return 0
        time.sleep(max(args.poll_seconds, 1.0))

if __name__ == "__main__":
    raise SystemExit(main())
