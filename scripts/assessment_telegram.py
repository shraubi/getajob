"""Send a private, rate-limited production assessment through the existing bot."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.acceptance_report import assess


def _load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _signature(report: dict, bot_health: str, release_status: str) -> str:
    return json.dumps(
        {
            "bot_health": bot_health,
            "release_status": release_status,
            "status": report["status"],
            "reasons": report["reasons"],
            "application_gate_closed": report["application_gate_closed"],
        },
        sort_keys=True,
    )


def should_send(report: dict, bot_health: str, release_status: str,
                state: dict, now: datetime) -> bool:
    if not state:
        return True
    if state.get("signature") != _signature(report, bot_health, release_status):
        return True
    try:
        last_sent = datetime.fromisoformat(state["last_sent_at"])
        if last_sent.tzinfo is None:
            return True
        elapsed = now - last_sent.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return True
    if elapsed >= timedelta(hours=24):
        return True
    held_increased = report["held_offer_count"] > int(state.get("held_offer_count", 0))
    return elapsed >= timedelta(hours=6) and (held_increased or bot_health != "healthy")


def format_message(report: dict, bot_health: str, release_status: str,
                   now: datetime) -> str:
    emails = report["email_statuses"]
    offers = report["offer_statuses"]
    accepted = emails.get("queued", 0)
    rejected = emails.get("rejected", 0)
    health = "работает" if bot_health == "healthy" else f"проблема ({bot_health})"
    lines = [
        f"getajob · отчёт {now:%d.%m %H:%M} UTC",
        f"Бот: {health}.",
        f"Версия на сервере: {'актуальная' if release_status == 'current' else 'отстаёт от main'}.",
        f"За 24 ч: писем принято {accepted}, отклонено {rejected}; "
        f"вакансий в held {offers.get('held', 0)}.",
        f"Подтверждённых новых откликов: {report['rechecked_new_submission_count']}. "
        f"Активная очередь: {report['active_offer_count']}.",
        f"Всего удержано вакансий: {report['held_offer_count']}.",
    ]
    if report["application_gate_closed"]:
        lines.append(
            "Автоотклики остановлены: лимит одной попытки уже использован. "
            "Новые вакансии удерживаются, пока ограничение не исправлено."
        )
    if release_status != "current":
        lines.append("Деплой не соответствует текущему main.")
    elif report["reasons"]:
        labels = {
            "offers_stuck_in_queue": "вакансии зависли в очереди",
            "emails_rejected": "есть отклонённые письма",
            "applications_failed": "есть ошибки отправки",
            "applications_need_attention": "есть отклики с неопределённым исходом",
            "offers_not_applied": "есть необработанные вакансии",
        }
        lines.append(
            "Проблема: " + ", ".join(labels.get(reason, reason) for reason in report["reasons"]) + "."
        )
    elif report["status"] == "RECHECKED_APPLIED":
        lines.append("Новые отклики подтверждены повторной проверкой в HelloWork.")
    elif report["status"] == "WAITING_FOR_LIVE_TRAFFIC":
        lines.append("Новых писем и вакансий за сутки не было.")
    else:
        lines.append("Новые отклики пока не подтверждены.")
    return "\n".join(lines)


async def _send(token: str, chat_id: int, message: str) -> None:
    from telegram import Bot

    async with Bot(token) as bot:
        await bot.send_message(chat_id=chat_id, text=message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--bot-health", choices=("healthy", "unhealthy", "starting", "missing", "unknown"),
                        default="unknown")
    parser.add_argument("--release-status", choices=("current", "stale"), default="stale")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    report = assess(args.db, since=now - timedelta(hours=24), now=now)
    state = _load_state(args.state)
    if not should_send(report, args.bot_health, args.release_status, state, now):
        print("telegram_assessment_report=SKIPPED")
        return 0
    try:
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = int(os.environ["YOUR_CHAT_ID"])
        asyncio.run(_send(token, chat_id, format_message(
            report, args.bot_health, args.release_status, now
        )))
        args.state.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.state.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "last_sent_at": now.isoformat(),
            "signature": _signature(report, args.bot_health, args.release_status),
            "held_offer_count": report["held_offer_count"],
        }), encoding="utf-8")
        os.replace(temporary, args.state)
    except Exception as exc:
        # Never print Telegram credentials or a response body in public Actions logs.
        print(f"telegram_assessment_report=FAIL reason={type(exc).__name__}")
        return 1
    print(f"telegram_assessment_report=SENT status={report['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

