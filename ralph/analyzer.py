"""Deterministic analysis of Jobbot conversations."""

from __future__ import annotations

import re
from datetime import timezone

from .models import ChatMessage, Finding, Interaction

_SUPPORT_MARKERS = (
    "technical support", "tech support", "support engineer", "support specialist",
    "customer support", "customer service", "customer care", "service desk",
    "help desk", "payment support", "support manager", "l1 support", "l2 support",
    "техническая поддержка", "технической поддержки", "техподдержка", "саппорт",
    "специалист поддержки", "инженер поддержки", "служба поддержки",
    "поддержка пользователей", "клиентская поддержка", "оператор поддержки",
)
_SUPPORTED_MARKERS = {
    "backend_python": ("python", "fastapi", "django", "flask"),
    "data_engineering": ("data engineer", "dataops", "airflow", "spark", "dbt", "etl"),
    "ml_engineering": ("machine learning", "ml engineer", "ai engineer", "llm", "mlops"),
    "devops": ("devops", "sre", "platform engineer", "kubernetes", "terraform"),
    "tech_support": _SUPPORT_MARKERS,
}
_BLOCKERS = {
    "authentication": ("authentication", "sign in", "login required", "otp"),
    "captcha": ("captcha",),
    "javascript": ("javascript", "js-only", "browser required"),
    "required_fields": ("missing required fields", "required screening", "required field"),
    "submission": ("application failed:", "send failed:"),
}
_THROTTLE_MARKERS = (
    "paused until", "minimum interval", "hourly telegram limit", "peerflood",
    "restricted outbound", "sending is disabled", "another telegram application is sending",
)
_SUCCESS_MARKERS = ("sent to @", "application submitted", "applied through hirify")
_UNSUPPORTED = "does not match any of the available resumes"


def group_interactions(
    messages: tuple[ChatMessage, ...], *, seed_request: ChatMessage | None = None
) -> tuple[Interaction, ...]:
    interactions: list[Interaction] = []
    request = seed_request
    responses: list[ChatMessage] = []

    def flush() -> None:
        nonlocal request, responses
        if request is not None or responses:
            key = str(request.id if request else responses[0].id)
            interactions.append(Interaction(key, request, tuple(responses)))
        request = None
        responses = []

    for message in messages:
        if message.outgoing:
            flush()
            request = message
        else:
            responses.append(message)
    flush()
    return tuple(interactions)


def _finding(
    interaction: Interaction,
    rule_id: str,
    severity: str,
    summary: str,
    evidence: dict[str, object] | None = None,
) -> Finding:
    messages = interaction.messages
    return Finding(
        rule_id=rule_id,
        severity=severity,
        summary=summary,
        interaction_id=interaction.id,
        message_ids=tuple(message.id for message in messages),
        timestamps=tuple(message.date.astimezone(timezone.utc).isoformat() for message in messages),
        evidence=evidence or {},
    )


def _direction(text: str) -> str | None:
    match = re.search(r"(?im)^direction:\\s*([a-z_]+)\\s*$", text)
    return match.group(1).casefold() if match else None


def _expected_direction(text: str) -> str | None:
    matches = [
        direction for direction, markers in _SUPPORTED_MARKERS.items()
        if any(marker in text for marker in markers)
    ]
    return matches[0] if len(matches) == 1 else None


def analyze_interactions(interactions: tuple[Interaction, ...]) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    throttled: list[Interaction] = []
    later_success = False

    for interaction in interactions:
        request_text = (interaction.request.text if interaction.request else "").casefold()
        response_text = "\n".join(message.text for message in interaction.responses).casefold()
        combined = f"{request_text}\n{response_text}"
        if "http 404" in combined or "page is no longer available" in combined:
            continue

        direction = _direction(response_text)
        expected = _expected_direction(combined)
        unsupported = _UNSUPPORTED in response_text
        has_document = any(message.has_document for message in interaction.responses)
        buttons = tuple(button.casefold() for message in interaction.responses for button in message.buttons)
        has_preview = any(
            marker in response_text
            for marker in ("recruiter message:", "no cover message", "selected resume")
        ) or has_document
        has_application_path = any(
            marker in response_text for marker in ("apply:", "contact: @")
        ) or any("apply" in button or "send" in button for button in buttons)

        if not interaction.responses and interaction.request:
            findings.append(_finding(
                interaction, "bot_response_missing", "high",
                "Jobbot produced no response for this interaction",
                {"response_count": 0},
            ))
            continue

        if interaction.request and interaction.responses:
            delay = (interaction.responses[0].date - interaction.request.date).total_seconds()
            if delay > 30:
                findings.append(_finding(
                    interaction, "bot_response_delayed", "low",
                    "Jobbot took more than 30 seconds to begin responding",
                    {"delay_seconds": int(delay)},
                ))

        if any(marker in combined for marker in _SUPPORT_MARKERS) and (
            unsupported or (direction is not None and direction != "tech_support")
        ):
            findings.append(_finding(
                interaction, "support_role_misclassified", "high",
                "A support role was rejected or classified outside tech_support",
                {"actual_direction": direction or "other", "unsupported": unsupported},
            ))
        elif expected and unsupported:
            findings.append(_finding(
                interaction, "supported_role_rejected", "high",
                "A role matching a supported résumé direction was rejected",
                {"expected_direction": expected},
            ))

        if expected and not unsupported and not has_document:
            findings.append(_finding(
                interaction, "resume_preview_missing", "medium",
                "A supported interaction did not produce a résumé document",
                {"expected_direction": expected, "has_document": False},
            ))

        blocker_types = sorted(
            name for name, markers in _BLOCKERS.items()
            if any(marker in response_text for marker in markers)
        )
        if blocker_types:
            findings.append(_finding(
                interaction, "application_blocked", "high",
                "The application path ended in a known blocker",
                {"blocker_types": blocker_types},
            ))
        elif has_preview and not has_application_path:
            findings.append(_finding(
                interaction, "application_path_missing", "medium",
                "The bot produced a résumé preview without an application or contact action",
                {"has_preview": True, "has_application_path": False},
            ))

        if any(marker in response_text for marker in _THROTTLE_MARKERS):
            throttled.append(interaction)
            findings.append(_finding(
                interaction, "telegram_throttled", "medium",
                "Telegram sending was blocked by a safety limit or cooldown",
                {"queue_present": False},
            ))
        if any(marker in response_text for marker in _SUCCESS_MARKERS):
            later_success = True

    if throttled and not later_success:
        last = throttled[-1]
        findings.append(_finding(
            last, "telegram_queue_missing", "high",
            "A throttled Telegram send had no later successful outcome or queue",
            {"throttled_interactions": len(throttled), "later_success": False},
        ))
    return tuple(findings)
