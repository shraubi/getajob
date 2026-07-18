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

It reviews at most 30 messages after the latest outgoing `Ralph-Run: <uuid>` marker and writes only structured findings and a checkpoint under `storage/`. If no marker exists, provide `--since <ISO timestamp>`.
