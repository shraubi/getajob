"""Read and analyze Jobbot's structured operational journal."""
from __future__ import annotations
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from .models import Finding

_AI_ROLE_MARKERS = (
    "vibe coder", "vibe coding", "ai-assisted developer",
    "ai-assisted development", "ai powered development",
)

_SUPPORT_MARKERS = (
    "technical support", "tech support", "support engineer", "support specialist",
    "customer support", "customer service", "customer care", "service desk",
    "help desk", "payment support", "support manager", "l1 support", "l2 support",
    "техническая поддержка", "технической поддержки", "техподдержка", "саппорт",
    "специалист поддержки", "инженер поддержки", "служба поддержки",
    "поддержка пользователей", "клиентская поддержка", "оператор поддержки",
)

@dataclass(frozen=True)
class OperationalEvent:
    id: int
    interaction_id: str
    event_type: str
    occurred_at: str
    data: dict[str, object]

def read_event_batch(db_path: Path, *, after_id: int, limit: int = 30) -> tuple[tuple[OperationalEvent, ...], bool]:
    if not db_path.is_file():
        return (), False
    connection = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=10
    )
    try:
        try:
            rows = connection.execute(
                """SELECT id, interaction_id, event_type, occurred_at, data_json
                   FROM jobbot_review_events WHERE id > ? ORDER BY id LIMIT ?""",
                (after_id, limit + 1),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).casefold():
                return (), False
            raise
    finally:
        connection.close()
    has_more = len(rows) > limit
    events = tuple(
        OperationalEvent(int(row[0]), str(row[1]), str(row[2]), str(row[3]), dict(json.loads(row[4])))
        for row in rows[:limit]
    )
    return events, has_more

def event_urls(events: tuple[OperationalEvent, ...]) -> tuple[str, ...]:
    urls: list[str] = []
    for event in events:
        value = event.data.get("source_url")
        if isinstance(value, str) and value.startswith(("http://", "https://")) and value not in urls:
            urls.append(value)
    return tuple(urls)

def _finding(event: OperationalEvent, rule_id: str, severity: str, summary: str,
             evidence: dict[str, object]) -> Finding:
    normalized = dict(evidence)
    source_url = event.data.get("source_url")
    if isinstance(source_url, str) and source_url.startswith(("http://", "https://")):
        normalized["urls"] = [source_url]
    return Finding(
        rule_id=rule_id, severity=severity, summary=summary,
        interaction_id=event.interaction_id, message_ids=(event.id,),
        timestamps=(event.occurred_at,), evidence=normalized,
    )

def analyze_events(events: tuple[OperationalEvent, ...]) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for event in events:
        data = event.data
        if bool(data.get("expired")):
            continue
        duration_ms = int(data.get("duration_ms") or 0)
        if duration_ms > 30_000:
            findings.append(_finding(
                event, "bot_response_delayed", "low",
                "Jobbot took more than 30 seconds to complete the interaction",
                {"delay_seconds": duration_ms // 1000},
            ))
        if event.event_type == "role_rejected":
            title = str(data.get("title") or "").casefold()
            scores = data.get("scores") if isinstance(data.get("scores"), dict) else {}
            expected = max(scores, key=scores.get) if scores and max(scores.values()) > 0 else None
            if expected is None and any(marker in title for marker in _AI_ROLE_MARKERS):
                expected = "ml_engineering"
            if any(marker in title for marker in _SUPPORT_MARKERS):
                findings.append(_finding(
                    event, "support_role_misclassified", "high",
                    "A support role was rejected as unsupported",
                    {"actual_direction": "other", "unsupported": True, "title": str(data.get("title") or "")},
                ))
            elif expected:
                findings.append(_finding(
                    event, "supported_role_rejected", "high",
                    "A role with a supported direction score was rejected",
                    {"expected_direction": expected, "title": str(data.get("title") or "")},
                ))
        if event.event_type == "resume_missing" or (
            event.event_type == "job_previewed" and not bool(data.get("resume_preview"))
        ):
            findings.append(_finding(
                event, "resume_preview_missing", "medium",
                "A supported interaction did not produce a résumé document",
                {"direction": data.get("direction") or "unknown"},
            ))
        if event.event_type == "job_previewed" and not bool(data.get("application_path")):
            findings.append(_finding(
                event, "application_path_missing", "medium",
                "The bot produced a résumé preview without an application or contact action",
                {"has_preview": True, "has_application_path": False},
            ))
        if event.event_type in {"job_fetch_failed", "application_failed"}:
            findings.append(_finding(
                event, "application_blocked", "high",
                "The application path ended in a known blocker",
                {"blocker_types": [str(data.get("blocker_type") or event.event_type)]},
            ))
        if event.event_type == "telegram_throttled":
            reason = str(data.get("reason") or "telegram_limit")
            findings.append(_finding(
                event, "telegram_throttled", "medium",
                "Telegram sending was blocked by a safety limit or cooldown",
                {"reason": reason, "queue_present": bool(data.get("queue_present"))},
            ))
            if not bool(data.get("queue_present")):
                findings.append(_finding(
                    event, "telegram_queue_missing", "high",
                    "A throttled Telegram send was not queued for a later attempt",
                    {"reason": reason, "queue_present": False},
                ))
    return tuple(findings)
