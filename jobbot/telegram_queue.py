"""Durable, serialized delivery for Telegram recruiter applications."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from jobbot import config
from jobbot.integrations.telegram_sender import (
    TelegramPeerFloodError,
    TelegramSender,
    TelegramSenderError,
)
from jobbot.review_events import record_review_event
from jobbot.store import (
    claim_telegram_job_for_send,
    complete_telegram_job,
    defer_telegram_job,
    get_due_telegram_job,
    mark_job_sent,
    pause_telegram_job,
    record_send_attempt,
    set_sender_cooldown,
)

logger = logging.getLogger(__name__)


async def _notify(bot, chat_id: int, text: str) -> None:
    if not bot or not chat_id:
        return
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        logger.exception("Could not send Telegram queue status to chat %s", chat_id)


def _review(job: dict, event_type: str, **data: object) -> None:
    interaction_id = str(job.get("queue_interaction_id") or f"queue:{job['id'][:24]}")
    record_review_event(
        config.JOBS_DB_PATH,
        interaction_id,
        event_type,
        source_url=job["page_url"],
        **data,
    )


async def process_telegram_queue_once(bot=None) -> bool:
    """Attempt one due Telegram application; return whether one was claimed."""
    job = get_due_telegram_job(config.JOBS_DB_PATH)
    if not job:
        return False

    claimed, retry_at, reason = claim_telegram_job_for_send(
        config.JOBS_DB_PATH,
        job["id"],
        min_interval_seconds=config.TELEGRAM_SEND_MIN_INTERVAL_SECONDS,
        max_per_hour=config.TELEGRAM_SEND_MAX_PER_HOUR,
    )
    if not claimed:
        if retry_at:
            defer_telegram_job(
                config.JOBS_DB_PATH,
                job["id"],
                available_at=retry_at,
                reason=reason,
            )
            record_send_attempt(
                config.JOBS_DB_PATH,
                job["id"],
                "telegram",
                job["contact_value"],
                "queued",
            )
            _review(
                job,
                "telegram_throttled",
                reason=reason,
                queue_present=True,
            )
            logger.info(
                "Telegram queue deferred job=%s until=%s reason=%s",
                job["id"],
                retry_at.isoformat(),
                reason,
            )
        return False

    notify_chat_id = int(job.get("queue_notify_chat_id") or 0)
    try:
        if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
            raise TelegramSenderError("Telegram sender is not configured")
        sender = TelegramSender(
            config.TELEGRAM_API_ID,
            config.TELEGRAM_API_HASH,
            config.TELEGRAM_SESSION_PATH,
        )
        external_id = await sender.send_resume(
            job["contact_value"],
            job["recruiter_message"],
            config.RESUME_DIR / job["resume_name"],
        )
        mark_job_sent(config.JOBS_DB_PATH, job["id"], external_id)
        record_send_attempt(
            config.JOBS_DB_PATH,
            job["id"],
            "telegram",
            job["contact_value"],
            "sent",
        )
        complete_telegram_job(config.JOBS_DB_PATH, job["id"])
        _review(job, "application_sent", channel="telegram", queue_present=True)
        await _notify(
            bot,
            notify_chat_id,
            f"Queued application sent to @{job['contact_value'].lstrip('@')} "
            f"with {job['resume_name']} (message {external_id}).",
        )
        logger.info(
            "Telegram queue sent job=%s target=@%s message=%s",
            job["id"],
            job["contact_value"].lstrip("@"),
            external_id,
        )
        return True
    except TelegramPeerFloodError as exc:
        blocked_until = datetime.now(timezone.utc) + timedelta(
            hours=config.TELEGRAM_PEER_FLOOD_COOLDOWN_HOURS
        )
        set_sender_cooldown(
            config.JOBS_DB_PATH,
            "telegram",
            blocked_until,
            "Telegram PeerFlood",
        )
        pause_telegram_job(
            config.JOBS_DB_PATH,
            job["id"],
            reason="peer_flood",
            error=exc,
        )
        record_send_attempt(
            config.JOBS_DB_PATH,
            job["id"],
            "telegram",
            job["contact_value"],
            "peer_flood_queued",
            exc,
        )
        _review(
            job,
            "telegram_throttled",
            reason="peer_flood",
            queue_present=True,
        )
        await _notify(
            bot,
            notify_chat_id,
            "Telegram restricted outbound messages. The application is paused in "
            "the queue and will not retry automatically.",
        )
        logger.warning(
            "Telegram PeerFlood; paused queued job=%s cooldown_until=%s",
            job["id"],
            blocked_until.isoformat(),
        )
        return True
    except Exception as exc:
        pause_telegram_job(
            config.JOBS_DB_PATH,
            job["id"],
            reason="send_error",
            error=exc,
        )
        record_send_attempt(
            config.JOBS_DB_PATH,
            job["id"],
            "telegram",
            job["contact_value"],
            "failed_queued",
            exc,
        )
        _review(
            job,
            "telegram_send_paused",
            reason=type(exc).__name__,
            queue_present=True,
        )
        await _notify(
            bot,
            notify_chat_id,
            f"Telegram send failed ({type(exc).__name__}); the application is paused "
            "in the queue and will not retry automatically.",
        )
        logger.exception(
            "Telegram queue send failed job=%s; automatic retry paused",
            job["id"],
        )
        return True


async def telegram_queue_worker(bot=None) -> None:
    logger.info(
        "Telegram queue worker started poll_seconds=%s",
        config.TELEGRAM_QUEUE_POLL_SECONDS,
    )
    while True:
        try:
            processed = await process_telegram_queue_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            processed = False
            logger.exception("Telegram queue worker iteration failed")
        if not processed:
            await asyncio.sleep(max(config.TELEGRAM_QUEUE_POLL_SECONDS, 1.0))
