# getajob

A deterministic Telegram bot for turning job posts into safe, resume-aware application drafts. The production path uses local rules and explicit user confirmation; it makes no LLM or embedding calls.

## What it does

1. Accepts pasted text or a public job URL in Telegram.
2. Parses and classifies the vacancy with deterministic rules.
3. Selects the best PDF from `data/resumes/`.
4. Builds a recruiter message and an application preview.
5. Applies only after an explicit Telegram button click.
6. Persists idempotency and send-rate state in SQLite.

## Repository map

```text
getajob/
├── jobbot/                 production Python package
│   ├── app.py              Telegram application wiring
│   ├── handlers.py         message and confirmation flows
│   ├── application.py      vacancy parsing and resume selection
│   ├── classifier.py       deterministic direction scoring
│   ├── store.py            SQLite persistence and rate limits
│   └── integrations/       job pages, Hirify, Telegram, web forms
├── tests/                  active production tests
├── scripts/                operator utilities
├── docs/                   architecture and deployment notes
├── archive/legacy_llm/     frozen pre-deterministic implementation
├── data/resumes/           private PDFs mounted at runtime
├── main.py                 production entry point
└── Dockerfile              production image
```

## Quick start

```bash
cp .env.example .env
# Set TELEGRAM_BOT_TOKEN and YOUR_CHAT_ID.
pip install -r requirements.txt
python main.py
```

For Docker:

```bash
docker compose up -d --build
```

Put private resume PDFs in `data/resumes/`. They and all runtime state under `storage/` are ignored by Git.

## Safety defaults

- Only the owner and IDs explicitly listed in `ADDITIONAL_CHAT_IDS` can use the bot.
- Telegram recruiter sends require `TELEGRAM_SENDING_ENABLED=true` and a confirmation click.
- Send pacing and PeerFlood cooldowns are persisted in SQLite.
- Web applications require a confirmation click and a positive success response.
- Deployment resets tracked VM files to GitHub `main`; runtime data remains untracked.

See [architecture](docs/architecture.md), [operations](docs/operations.md), and the [legacy archive](archive/legacy_llm/README.md).
