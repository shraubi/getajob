# Ralph Loop - Unattended Jobbot Testing

The `loop.py` module implements the unattended "away mode" for Ralph, enabling periodic testing of Jobbot's workflow without manual intervention.

## Overview

Ralph Loop:
1. **Discovers** fresh jobs from configured sources (currently Hirify)
2. **Filters** out URLs already processed and stored in `storage/ralph.db`
3. **Tests** each job through Jobbot via Telegram (or direct rating)
4. **Detects** failures in parsing, classification, applicant profile, and application preview
5. **Reports** failures by creating/updating GitHub issues
6. **Never** clicks application buttons or submits resumes

## Usage

### Basic Loop

```bash
# Run with default settings (Hirify, limit 10)
python -m ralph.loop --filter hirify --limit 10

# With custom feed URL
python -m ralph.loop --filter hirify --feed-url "https://hirify.me/jobs?filter=data" --limit 5

# Dry run (no GitHub issues, no Telegram)
python -m ralph.loop --filter hirify --limit 5 --dry-run

# Quiet mode (only JSON output)
python -m ralph.loop --filter hirify --limit 5 --quiet
```

### With Telegram Integration

```bash
# Full loop with Telegram
python -m ralph.loop \
  --filter hirify \
  --limit 10 \
  --session-path storage/telegram_sender \
  --db storage/ralph.db

# Requires environment variables:
# - TELEGRAM_API_ID
# - TELEGRAM_API_HASH
# - TELEGRAM_BOT_TOKEN
```

### With GitHub Integration

```bash
# Full loop with GitHub issue creation
python -m ralph.loop \
  --filter hirify \
  --limit 10 \
  --db storage/ralph.db \
  --session-path storage/telegram_sender

# Requires environment variables:
# - GITHUB_REPOSITORY (e.g., "owner/repo")
# - GITHUB_TOKEN (with repo scope)
```

### Scheduling with Cron

To run every 6 hours on a VM:

```bash
# Add to crontab (crontab -e)
0 */6 * * * cd /path/to/getajob && /usr/bin/docker compose exec -T bot python -m ralph.loop --filter hirify --limit 10 --db storage/ralph.db --session-path storage/telegram_sender
```

Or with a systemd timer:

```ini
# /etc/systemd/system/ralph-loop.service
[Unit]
Description=Ralph Jobbot Testing Loop

[Service]
Type=oneshot
WorkingDirectory=/path/to/getajob
ExecStart=/usr/bin/docker compose exec -T bot python -m ralph.loop --filter hirify --limit 10
EnvironmentFile=/path/to/getajob/.env

# /etc/systemd/system/ralph-loop.timer
[Unit]
Description=Run Ralph loop every 6 hours

[Timer]
OnCalendar=*-*-* 0/6:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

## Stages and Scoring

Each job is rated across 4 stages (110 points total):

1. **parser** (40 points): Job page parsing (title, company, description)
2. **classification** (30 points): Role classification matches expected direction
3. **applicant_profile** (20 points): Profile fields populated correctly (name, phone, LinkedIn)
4. **application** (20 points): Application preview available (no submission)

A job **passes** only if all stages pass. Any failure creates a GitHub issue.

## Failure Detection

### Applicant Profile Errors

Ralph now detects when Jobbot fails to populate required applicant profile fields:

```
Application failed: Applicant profile is missing required fields: name, phone, urls[LinkedIn]
```

This creates a failure in the `applicant_profile` stage with evidence containing:
- `error_type`: "applicant_profile_missing_fields"
- `missing_fields`: ["name", "phone", "urls[LinkedIn]"]
- `error_message`: Full error text

**Important**: The fix for this error belongs in Jobbot's applicant-profile/form-filling logic, not in Ralph. Ralph only reports the issue.

## Database Schema

The loop uses `storage/ralph.db` with two tables:

### ralph_runs
- `id`: Unique run identifier
- `url`: Job URL
- `domain`: Job domain
- `score`: Total score (0-110)
- `status`: "passed" or "failed"
- `report_json`: Full rating report as JSON
- `created_at`: Timestamp

### ralph_failures
- `fingerprint`: Unique hash of (domain, stage, summary)
- `run_id`: Associated run
- `url`: Job URL
- `domain`: Job domain
- `stage`: Failing stage name
- `summary`: Failure summary
- `evidence_json`: Failure evidence as JSON
- `occurrences`: Number of times this failure has been seen
- `issue_number`: GitHub issue number (if created)
- `status`: "queued", "issue-open", etc.
- `first_seen_at`: First occurrence timestamp
- `last_seen_at`: Last occurrence timestamp

## GitHub Issues

Each unique failure (by fingerprint) creates at most one GitHub issue. Issues are:
- **Created** when a new failure is detected
- **Updated** when the same failure recurs (with new evidence)
- **Closed** manually when the underlying Jobbot issue is fixed

Issue titles follow the pattern: `[Ralph][stage] domain: summary`

Issue bodies include:
- Run ID and timestamps
- Job URL, title, company
- Rating score
- Failure stage and summary
- Evidence JSON
- Reproduction command
- Acceptance criteria checklist

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_API_ID` | No* | Telegram API ID for bot interaction |
| `TELEGRAM_API_HASH` | No* | Telegram API hash |
| `TELEGRAM_BOT_TOKEN` | No* | Telegram bot token |
| `RALPH_TELEGRAM_SESSION_PATH` | No* | Path to Telegram session file |
| `GITHUB_REPOSITORY` | No | GitHub repo in "owner/name" format |
| `GITHUB_TOKEN` | No | GitHub personal access token |

*Required for Telegram flow; without them, direct rating is used.

## Docker Integration

The loop is designed to run inside the bot container:

```bash
# One-shot test
docker compose exec -T bot python -m ralph.loop --filter hirify --limit 5 --dry-run

# Full production run
docker compose exec -T bot python -m ralph.loop --filter hirify --limit 10
```

## Key Design Principles

1. **Observation Only**: Ralph never submits applications or clicks buttons
2. **Deterministic**: Same job URL always produces the same rating
3. **Non-Invasive**: Uses existing Jobbot infrastructure (Telegram)
4. **GitHub as Dashboard**: All failures tracked via GitHub issues
5. **Jobbot Owns Fixes**: Ralph reports, Jobbot fixes

## Troubleshooting

### "No module named 'ralph'"

Ensure you're running from the project root or have the package installed:

```bash
cd /path/to/getajob
pip install -e .
```

### Telegram connection errors

Verify:
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_BOT_TOKEN` are set
- Session file exists at the specified path
- Bot is running and accessible

### GitHub issue creation fails

Verify:
- `GITHUB_TOKEN` has `repo` scope
- `GITHUB_REPOSITORY` is in "owner/name" format
- Token is not expired

### Jobs not being discovered

Check:
- Feed URL is accessible
- HTML structure hasn't changed (update `extract_job_urls` if needed)
- No network restrictions blocking requests
