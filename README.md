# GetAJob

A Telegram bot that scores job vacancies using Claude AI and generates a tailored CV and recruiter message for matches.

**Workflow:** paste a vacancy → Claude scores it (0–10) → if score ≥ 6, get a custom CV (PDF) + outreach message → send, edit, or skip.

---

## Prerequisites

- Docker and Docker Compose
- A Telegram bot token
- An Anthropic API key

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/shraubi/getajob.git
cd getajob
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `ANTHROPIC_API_KEY` | Key from console.anthropic.com |
| `YOUR_CHAT_ID` | Your Telegram user ID |

---

## Running

### With Docker Compose (recommended)

**Using `cv/base_cv.txt` (default):** replace the file content with your own CV, then:

```bash
docker compose up --build -d
```

**Using a CV file stored elsewhere on the VM:**

```bash
CV_FILE=/path/to/mycv.txt docker compose up --build -d
```

View logs:

```bash
docker compose logs -f
```

Stop:

```bash
docker compose down
```

### With plain Docker

```bash
docker build -t getajob .
docker run --env-file .env -e CV="$(cat /path/to/mycv.txt)" getajob
```

### Without Docker

```bash
pip install -r requirements.txt
python main.py
```

---

## Project Structure

```
getajob/
├── main.py          # Entry point
├── config.py        # Env var loading
├── bot/
│   ├── claude_client.py   # Claude scoring + generation
│   └── handlers.py        # Telegram message handlers
├── cv/
│   ├── base_cv.txt        # Default CV template
│   └── renderer.py        # PDF generation
└── storage/
    └── state.py           # In-memory state for pending actions
```
