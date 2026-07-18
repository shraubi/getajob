# Operations

## Runtime data

Keep mutable production data only in these untracked locations:

- `.env`
- `storage/`
- `data/resumes/`

Do not edit tracked source files on the VM. Deployment treats GitHub `main` as the source of truth and runs `git reset --hard origin/main` before rebuilding the container. This prevents VM drift from blocking releases.

## Deploy

A push to `main` runs tests and one serialized production deploy. The deploy:

1. fetches `main`;
2. resets tracked files to the fetched commit;
3. rebuilds the lightweight image;
4. restarts the bot;
5. prunes unused image layers.

The workflow can also be started manually from GitHub Actions.

## Rollback

Revert the faulty commit on `main`. The resulting single push redeploys the reverted source. Preserve `storage/`, `.env`, and `data/resumes/` during rollback.

## Manual Ralph review

Ralph is a standalone, read-only reviewer. It reuses the authorized Telegram user session but does not send messages, click buttons, apply to jobs, or write to GitHub.

```bash
docker compose exec -T bot python -m ralph.review_chat
```

It reviews history in chronological chunks of 30 after the saved checkpoint or latest outgoing `Ralph-Run: <uuid>` marker. When more messages remain, run the same command again; no timestamp is needed. Without either boundary it reviews the most recent 30 messages. Reports include detected job links but never transcript text.

## Continuous cloud Ralph review

The `ralph` Compose service continuously reads Jobbot's structured operational journal from `storage/jobs.db`. It never logs into a Telegram user account and never sends messages. It writes findings to `storage/ralph.db` and `storage/ralph/reviews/`.

    docker compose logs -f ralph
    docker compose exec -T ralph python -m ralph.watch_events --once

Every account using Jobbot must be listed in `YOUR_CHAT_ID` or `ADDITIONAL_CHAT_IDS`. Jobbot records normalized outcomes, links, classifications, preview/application availability, failures and throttles. It does not persist raw Telegram transcript or document contents.

### GitHub issue publishing

Set `RALPH_GITHUB_REPOSITORY` and a fine-grained `RALPH_GITHUB_TOKEN` with Issues write permission. Ralph creates deduplicated issues for medium/high findings and keeps failed deliveries in its SQLite outbox for retry. Set `RALPH_GITHUB_MIN_SEVERITY=high` to reduce issue volume.
