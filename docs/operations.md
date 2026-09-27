# Operations

## Runtime data

Keep mutable production data only in these untracked locations:

- `.env`
- `storage/`
- `data/resumes/`

Do not edit tracked source files on the VM. Deployment treats GitHub `main` as the source of truth and runs `git reset --hard origin/main` before rebuilding the container. This prevents VM drift from blocking releases.

## Deploy

The `Delivery` workflow tests every pull request. A push to `main` or a manual `Delivery` run deploys only after the same commit passes tests. The deploy:

1. fetches `main`;
2. resets tracked files to the fetched commit;
3. rebuilds the lightweight image;
4. restarts the bot and checks authenticated Telegram readiness;
5. sends one `/health` nonce from the existing authorized Telegram user session and checks the bot's reply;
6. prints a content-free HelloWork state report;
7. prunes unused image layers.

Start `Delivery` manually from GitHub Actions to repeat both tests and deployment. The separate `Production assessment` workflow runs hourly and can also be started manually. It checks the deployed SHA, container health, and the last 24 hours of HelloWork queue outcomes. GitHub Actions job logs are the operator report; no Telegram or VM log copying is required.

The Telegram probe requires the existing user session, `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH` on the VM. It sends one private `/health` command to the bot and never clicks an application button. If the session is missing or unauthorized, deployment fails with a token-free reason.

HelloWork report states are `FAIL`, `WAITING_FOR_LIVE_TRAFFIC`, `WAITING_FOR_TERMINAL_OUTCOME`, and `RECORDED_COMPLETION`. `RECORDED_COMPLETION` means the bot persisted a completed offer; it is not yet an independent HelloWork account confirmation. The report never includes email text, URLs, profile fields, or raw exception messages.

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

Set `GITHUB_REPOSITORY` and a fine-grained `GITHUB_TOKEN` with Issues write permission. Ralph creates deduplicated issues for medium/high findings and keeps failed deliveries in its SQLite outbox for retry.

### Browser storage

The bot image contains browser system libraries only. The deploy downloads Chromium Headless Shell once into `storage/playwright/`, outside Docker's compressed image layers. This avoids BuildKit temporarily storing multiple copies of the browser during image export.
