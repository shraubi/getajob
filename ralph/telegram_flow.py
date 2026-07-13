"""Send a job through the deployed Telegram bot and rate its observed output."""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from .rating import RatingReport, StageRating


class TelegramFlowError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObservedMessage:
    text: str
    has_document: bool = False
    buttons: tuple[str, ...] = ()


async def resolve_bot_username(bot_token: str) -> str:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"https://api.telegram.org/bot{bot_token}/getMe")
        response.raise_for_status()
    payload = response.json()
    username = str(payload.get("result", {}).get("username", "")).strip()
    if not payload.get("ok") or not username:
        raise TelegramFlowError("Telegram Bot API did not return the Jobbot username")
    return username


def _observed(message) -> ObservedMessage:
    buttons = tuple(
        str(button.text)
        for row in (getattr(message, "buttons", None) or [])
        for button in row
        if getattr(button, "text", None)
    )
    return ObservedMessage(
        text=str(getattr(message, "message", "") or ""),
        has_document=bool(getattr(message, "document", None)),
        buttons=buttons,
    )


def _next_response_timeout(
    observed_count: int, *, quiet_seconds: float, timeout_seconds: float
) -> float:
    return timeout_seconds if observed_count == 0 else quiet_seconds


async def send_job_to_bot(
    url: str,
    *,
    api_id: int,
    api_hash: str,
    session_path: Path,
    bot_token: str,
    quiet_seconds: float = 12.0,
    timeout_seconds: float = 75.0,
) -> tuple[str, tuple[ObservedMessage, ...]]:
    run_id = uuid.uuid4().hex
    bot_username = await resolve_bot_username(bot_token)
    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise TelegramFlowError(f"Telegram user session is not authorized: {session_path}")
        entity = await client.get_entity(bot_username)
        observed: list[ObservedMessage] = []
        async with client.conversation(entity, timeout=timeout_seconds) as conversation:
            await conversation.send_message(f"{url}\n\nRalph-Run: {run_id}")
            while True:
                try:
                    response_timeout = _next_response_timeout(
                        len(observed),
                        quiet_seconds=quiet_seconds,
                        timeout_seconds=timeout_seconds,
                    )
                    response = await asyncio.wait_for(
                        conversation.get_response(), timeout=response_timeout
                    )
                except asyncio.TimeoutError:
                    break
                observed.append(_observed(response))
        if not observed:
            raise TelegramFlowError("Jobbot did not reply before the quiet timeout")
        return run_id, tuple(observed)
    except SessionPasswordNeededError as exc:
        raise TelegramFlowError("Telegram user session requires two-factor authentication") from exc
    finally:
        await client.disconnect()


def review_bot_output(
    url: str,
    messages: tuple[ObservedMessage, ...],
    *,
    expected_direction: str | None = None,
) -> RatingReport:
    combined = "\n".join(message.text for message in messages)
    role_match = re.search(r"(?m)^Role:\s*(.+)$", combined)
    company_match = re.search(r"(?m)^Company:\s*(.+)$", combined)
    direction_match = re.search(r"(?m)^Direction:\s*([a-z_]+)\s*$", combined)
    direction = direction_match.group(1) if direction_match else "other"
    parser_error = next(
        (line for line in combined.splitlines() if line.startswith(("Could not process the linked job:", "Error:"))),
        "",
    )
    unsupported = "does not match any of the available resumes" in combined
    # The unsupported-role response is emitted only after the linked page was
    # fetched and parsed into a Vacancy, even though the bot does not echo its
    # fields on this early-return path.
    parse_passed = bool((role_match and company_match) or unsupported) and not parser_error
    parser = StageRating(
        "parser", parse_passed, 40 if parse_passed else 0, 40,
        "Bot returned parsed role and company" if parse_passed else parser_error or "Bot did not return parsed role and company",
        {"role": role_match.group(1) if role_match else "", "company": company_match.group(1) if company_match else "", "error": parser_error},
    )

    classification_passed = (
        direction == expected_direction if expected_direction else bool(direction_match and direction != "other")
    ) and not unsupported
    classification = StageRating(
        "classification", classification_passed, 30 if classification_passed else 0, 30,
        f"Bot classified the role as {direction}" if classification_passed else f"Expected {expected_direction or 'a supported direction'}, observed {direction}",
        {
            "expected_direction": expected_direction,
            "actual_direction": direction,
            "unsupported_message": unsupported,
            "observed_bot_messages": [message.text[:1000] for message in messages],
        },
    )

    has_resume = any(message.has_document for message in messages)
    action_buttons = tuple(button for message in messages for button in message.buttons)
    has_application_result = any(
        marker in combined for marker in ("Contact: @", "Apply: ", "No cover message", "Recruiter message:")
    ) or bool(action_buttons)
    application_passed = parse_passed and classification_passed and has_resume and has_application_result
    application = StageRating(
        "application", application_passed, 30 if application_passed else 0, 30,
        "Bot produced a resume and non-submitting application preview" if application_passed else "Bot did not reach the resume/application preview",
        {"has_resume_document": has_resume, "buttons_observed_not_clicked": action_buttons, "has_application_result": has_application_result},
    )
    stages = (parser, classification, application)
    score = sum(stage.points for stage in stages)
    return RatingReport(
        url=url,
        domain=(urlparse(url).hostname or "telegram-jobbot").casefold(),
        title=role_match.group(1) if role_match else url,
        company=company_match.group(1) if company_match else "Unknown company",
        direction=direction,
        score=score,
        status="passed" if all(stage.passed for stage in stages) else "failed",
        stages=stages,
    )

