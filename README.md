# GetAJob

A Telegram bot that scores job vacancies using Claude AI and generates a tailored CV and recruiter message for matches.

**Workflow:** paste a vacancy → Claude scores it (0–10) → if score ≥ 6, get a custom CV (PDF) + outreach message → send, edit, or skip.

---

## Prerequisites

- Docker and Docker Compose
- A Telegram bot token ([create one via @BotFather](https://t.me/BotFather))
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
| `YOUR_CHAT_ID` | Your Telegram user ID (get it from @userinfobot) |

### 3. Add your CV

Replace `cv/base_cv.txt` with your own CV text. The bot uses this as the base when generating tailored versions.

Alternatively, set the `CV` environment variable to a path or raw CV text.

---

## Running

### With Docker Compose (recommended)

```bash
docker compose up --build -d
```

View logs:

```bash
docker compose logs -f
```

Stop:

```bash
docker compose down
```

### Without Docker

```bash
pip install -r requirements.txt
python main.py
```

---

## Automated Deployment (CI/CD)

Pushing to `main` triggers a GitHub Actions workflow that SSHes into your VM and redeploys automatically.

### One-time setup

**1. Generate an SSH key pair on your local machine** (skip if you already have one configured for the VM):

```bash
ssh-keygen -t ed25519 -C "github-deploy" -f ~/.ssh/deploy_key
```

Add the public key to your VM's `~/.ssh/authorized_keys`:

```bash
ssh-copy-id -i ~/.ssh/deploy_key.pub user@your-vm-ip
```

**2. Add the following secrets to your GitHub repo**

Go to *Settings → Secrets and variables → Actions → New repository secret*:

| Secret | Value |
|---|---|
| `VM_HOST` | Your VM's IP address or hostname |
| `VM_USER` | SSH username (e.g. `ubuntu`) |
| `VM_SSH_KEY` | Contents of `~/.ssh/deploy_key` (the private key) |
| `APP_DIR` | Absolute path to the repo on the VM (e.g. `/home/ubuntu/getajob`) |

**3. Make sure the repo is cloned on the VM and `.env` is in place**

```bash
ssh user@your-vm-ip
git clone https://github.com/shraubi/getajob.git /home/ubuntu/getajob
cd /home/ubuntu/getajob
cp .env.example .env
# edit .env with real values
```

After this, every `git push` to `main` will automatically redeploy the bot.

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
│   ├── base_cv.txt        # Your CV template
│   └── renderer.py        # PDF generation
└── storage/
    └── state.py           # In-memory state for pending actions
```
