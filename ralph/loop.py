"""Periodic Ralph loop: discover, test, and report Jobbot failures.

This module implements the unattended "away mode" for Ralph:
- Fetches fresh jobs using Hirify filter
- Skips URLs already recorded in storage/ralph.db
- Sends each job to the bot through Telegram
- Reviews parser, classification, and application-preview output
- Creates or updates GitHub issues for failures
- Never clicks an application button or submits a resume
- Runs periodically on the VM with strict job and time limits
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from .discover import extract_job_urls
from .rating import rate_job
from .store import record_report, render_issue, mark_issue_created
from .telegram_flow import review_bot_output, send_job_to_bot
from .github_issue import sync_issue


@dataclass(frozen=True)
class LoopConfig:
    """Configuration for a Ralph loop run."""
    filter_source: str = "hirify"
    feed_url: str = "https://hirify.me/"
    limit: int = 10
    db_path: Path = Path("storage/ralph.db")
    session_path: Path | None = None
    github_repository: str = ""
    github_token: str = ""
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_bot_token: str = ""
    dry_run: bool = False
    quiet: bool = False


def get_known_urls(db_path: Path) -> set[str]:
    """Return the set of URLs already recorded in the Ralph database."""
    if not db_path.exists():
        return set()
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT url FROM ralph_runs").fetchall()
        return {row[0] for row in rows}
    except sqlite3.OperationalError:
        return set()
    finally:
        connection.close()


def get_pending_failures(db_path: Path) -> list[dict[str, Any]]:
    """Return failures that need GitHub issues created."""
    if not db_path.exists():
        return []
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            """SELECT fingerprint, run_id, url, domain, stage, summary, evidence_json 
               FROM ralph_failures 
               WHERE status = 'queued' OR (status = 'issue-open' AND issue_number IS NULL)"""
        ).fetchall()
        return [
            {
                "fingerprint": row[0],
                "run_id": row[1],
                "url": row[2],
                "domain": row[3],
                "stage": row[4],
                "summary": row[5],
                "evidence_json": row[6],
            }
            for row in rows
        ]
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()


async def fetch_hirify_feed(feed_url: str = "https://hirify.me/", limit: int = 20) -> list[str]:
    """Fetch the Hirify feed and extract job URLs."""
    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "getajob-ralph/0.1 (+deterministic compatibility tester)"},
    ) as client:
        response = await client.get(feed_url)
        response.raise_for_status()
    urls = extract_job_urls(response.text, feed_url)
    return urls[:limit]


async def process_job(
    url: str,
    config: LoopConfig,
    *,
    expected_direction: str | None = None,
) -> dict[str, Any] | None:
    """Process a single job URL through the full Ralph pipeline.
    
    Returns a result dict with run_id, report, and any created issue info,
    or None if the job should be skipped.
    """
    if config.quiet:
        print(f"  Processing: {url}")
    
    # Check if we should use Telegram flow or direct rating
    # Use Telegram if all credentials are configured and session path exists
    use_telegram = (
        config.telegram_api_id and
        config.telegram_api_hash and
        config.telegram_bot_token and
        config.session_path and
        config.session_path.exists()
    )
    
    if use_telegram:
        # Use Telegram flow
        try:
            telegram_run_id, messages = await send_job_to_bot(
                url,
                api_id=config.telegram_api_id,
                api_hash=config.telegram_api_hash,
                session_path=config.session_path,
                bot_token=config.telegram_bot_token,
            )
            report = review_bot_output(url, messages, expected_direction=expected_direction)
            result: dict[str, Any] = {
                "url": url,
                "telegram_run_id": telegram_run_id,
                "report": report,
                "via_telegram": True,
            }
        except Exception as exc:
            if config.quiet:
                print(f"    Telegram flow failed: {exc}")
            # Fall back to direct rating
            report = await rate_job(url, expected_direction=expected_direction)
            result = {
                "url": url,
                "telegram_run_id": "",
                "report": report,
                "via_telegram": False,
                "telegram_error": str(exc),
            }
    else:
        # Direct rating only
        if config.quiet:
            reason = "dry-run" if config.dry_run else "Telegram not configured"
            print(f"    Using direct rating ({reason})")
        report = await rate_job(url, expected_direction=expected_direction)
        result = {
            "url": url,
            "telegram_run_id": "",
            "report": report,
            "via_telegram": False,
        }
    
    # Record the report (unless dry-run)
    if not config.dry_run:
        run_id, fingerprints = record_report(config.db_path, result["report"])
        result["run_id"] = run_id
        result["fingerprints"] = fingerprints
    else:
        result["run_id"] = "dry-run"
        result["fingerprints"] = []
    
    # Check for failures and create issues (unless dry-run)
    if result["report"].failures:
        failure = result["report"].failures[0]
        issue = render_issue(result["report"], result["run_id"], failure)
        result["issue"] = issue
        
        # If GitHub is configured and not dry-run, sync the issue
        if config.github_repository and config.github_token and not config.dry_run:
            try:
                gh_result = await sync_issue(
                    issue,
                    repository=config.github_repository,
                    token=config.github_token,
                )
                result["github_issue"] = gh_result
                # Mark as created in DB
                if not config.dry_run:
                    mark_issue_created(config.db_path, issue["fingerprint"], int(gh_result["number"]))
            except Exception as exc:
                if config.quiet:
                    print(f"    GitHub issue creation failed: {exc}")
                result["github_error"] = str(exc)
    
    return result


async def sync_pending_issues(config: LoopConfig) -> list[dict[str, Any]]:
    """Sync pending failures to GitHub issues."""
    if not config.github_repository or not config.github_token:
        if config.quiet:
            print("  Skipping GitHub sync: no repository/token configured")
        return []
    
    if config.dry_run:
        if config.quiet:
            print("  Skipping GitHub sync: dry run mode")
        return []
    
    pending = get_pending_failures(config.db_path)
    results = []
    
    for failure in pending:
        # Reconstruct the issue payload from the failure data
        issue_payload = {
            "title": f"[Ralph][{failure['stage']}] {failure['domain']}: {failure['summary']}",
            "body": f"""## Automated failure

- **Run:** `{failure['run_id']}`
- **Source:** `{failure['domain']}`
- **Job URL:** {failure['url']}
- **Stage:** `{failure['stage']}`
- **Observed:** {failure['summary']}
- **Fingerprint:** `{failure['fingerprint']}`

## Evidence

```json
{failure['evidence_json']}
```

## Reproduction

```bash
python -m ralph.first_loop --url "{failure['url']}"
```

## Acceptance criteria

- [ ] Reproduce the failure with a sanitized fixture.
- [ ] Add or improve the source-specific parser/application adapter.
- [ ] Add a regression test for this URL shape.
- [ ] Run `python -m unittest discover -s tests -v`.
- [ ] Rerun this Ralph rating and make the failing stage pass.

_Created deterministically by the Ralph rating chain; no LLM was used to rate or write this issue._
""",
            "fingerprint": failure["fingerprint"],
            "run_id": failure["run_id"],
        }
        
        try:
            gh_result = await sync_issue(
                issue_payload,
                repository=config.github_repository,
                token=config.github_token,
            )
            mark_issue_created(config.db_path, failure["fingerprint"], int(gh_result["number"]))
            results.append({
                "fingerprint": failure["fingerprint"],
                "github_issue": gh_result,
            })
            if config.quiet:
                print(f"    Created GitHub issue #{gh_result['number']} for {failure['fingerprint'][:12]}")
        except Exception as exc:
            if config.quiet:
                print(f"    Failed to create GitHub issue for {failure['fingerprint'][:12]}: {exc}")
            results.append({
                "fingerprint": failure["fingerprint"],
                "error": str(exc),
            })
    
    return results


async def run_loop(config: LoopConfig) -> dict[str, Any]:
    """Run the full Ralph loop: discover, process, report."""
    start_time = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    skipped: list[str] = []
    errors: list[dict[str, Any]] = []
    
    # Get known URLs to skip
    known_urls = get_known_urls(config.db_path)
    
    if config.quiet:
        print(f"Starting Ralph loop at {start_time.isoformat()}")
        if config.dry_run:
            print("  Mode: DRY-RUN (no GitHub issues, no DB writes)")
        print(f"Known URLs to skip: {len(known_urls)}")
    
    # Discover fresh jobs
    if config.filter_source == "hirify":
        try:
            urls = await fetch_hirify_feed(config.feed_url, config.limit)
        except Exception as exc:
            if config.quiet:
                print(f"Discovery failed: {exc}")
            return {
                "status": "failed",
                "error": str(exc),
                "started_at": start_time.isoformat(),
                "ended_at": datetime.now(timezone.utc).isoformat(),
            }
    else:
        raise ValueError(f"Unknown filter source: {config.filter_source}")
    
    if config.quiet:
        print(f"Discovered {len(urls)} job URLs")
    
    # Filter out known URLs
    new_urls = [url for url in urls if url not in known_urls]
    if config.quiet:
        print(f"New URLs to process: {len(new_urls)}")
    
    # Process each new job
    for url in new_urls:
        try:
            result = await process_job(url, config)
            if result:
                results.append(result)
                if config.quiet:
                    status = "PASS" if result["report"].status == "passed" else "FAIL"
                    via = "Telegram" if result.get("via_telegram") else "Direct"
                    print(f"    [{status}] {url} -> score={result['report'].score}, via={via}, failures={len(result['report'].failures)}")
        except Exception as exc:
            errors.append({"url": url, "error": str(exc)})
            if config.quiet:
                print(f"    [ERROR] {url}: {exc}")
    
    # Sync pending issues to GitHub
    if config.quiet:
        print("Syncing pending failures to GitHub...")
    gh_results = await sync_pending_issues(config)
    
    end_time = datetime.now(timezone.utc)
    
    return {
        "status": "completed",
        "started_at": start_time.isoformat(),
        "ended_at": end_time.isoformat(),
        "discovered": len(urls),
        "known": len(known_urls),
        "new": len(new_urls),
        "processed": len(results),
        "skipped": len(skipped),
        "errors": len(errors),
        "results": results,
        "skipped_urls": skipped,
        "processing_errors": errors,
        "github_issues": gh_results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Ralph loop: discover jobs, test Jobbot, report failures"
    )
    parser.add_argument(
        "--filter",
        choices=["hirify"],
        default="hirify",
        help="Job source filter (default: hirify)",
    )
    parser.add_argument(
        "--feed-url",
        default="https://hirify.me/",
        help="URL to fetch jobs from (default: https://hirify.me/)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of jobs to process (default: 10)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("storage/ralph.db"),
        help="Path to Ralph SQLite database (default: storage/ralph.db)",
    )
    parser.add_argument(
        "--session-path",
        type=Path,
        default=None,
        help="Path to Telegram session file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't create GitHub issues or write to DB (but still uses Telegram)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    parser.add_argument(
        "--expected-direction",
        default=None,
        help="Expected job direction for classification",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()
    
    # Resolve session path: check RALPH_TELEGRAM_SESSION_PATH, then TELEGRAM_SESSION_PATH, then default
    session_path_str = (
        os.environ.get("RALPH_TELEGRAM_SESSION_PATH") or
        os.environ.get("TELEGRAM_SESSION_PATH") or
        ""
    )
    session_path = args.session_path or (Path(session_path_str) if session_path_str else None)
    
    config = LoopConfig(
        filter_source=args.filter,
        feed_url=args.feed_url,
        limit=args.limit,
        db_path=args.db,
        session_path=session_path,
        github_repository=os.environ.get("GITHUB_REPOSITORY", ""),
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        telegram_api_id=int(os.environ.get("TELEGRAM_API_ID", "0")),
        telegram_api_hash=os.environ.get("TELEGRAM_API_HASH", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        dry_run=args.dry_run,
        quiet=not args.quiet,  # Invert: quiet flag suppresses output
    )
    
    # Validate Telegram config
    if config.telegram_api_id and config.telegram_api_hash and config.telegram_bot_token:
        if not config.session_path or not config.session_path.exists():
            if not args.quiet:
                print(f"Warning: Telegram session path not found or doesn't exist: {config.session_path}, will use direct rating only")
    else:
        if not args.quiet:
            print("Warning: Telegram credentials not fully configured, will use direct rating only")
    
    loop_result = asyncio.run(run_loop(config))
    
    # Always print final summary
    print(json.dumps(loop_result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
