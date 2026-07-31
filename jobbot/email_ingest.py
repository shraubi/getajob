"""Background Gmail intake and automatic HelloWork application processing."""

from __future__ import annotations

import asyncio
import logging
from email import policy
from email.parser import BytesParser

from jobbot import config
from jobbot.application import ResumeNotFoundError, UnknownDirectionError, build_application_for_vacancy
from jobbot.email_store import (
    claim_next_offer,
    finish_offer,
    record_email_offers,
    record_rejected_email,
)
from jobbot.form_answers import FormQuestion, create_answer_batch, set_batch_message_id
from jobbot.integrations.ats import (
    AtsError,
    fetch_ats_page,
    preflight_ats_application,
    submit_ats_application,
)
from jobbot.integrations.hellowork_email import (
    GmailInbox,
    HelloWorkEmailError,
    parse_hellowork_alert,
    resolve_alert_offers,
)
from jobbot.store import (
    claim_job_for_send,
    mark_job_awaiting_answers,
    mark_job_send_failed,
    mark_job_sent,
    record_send_attempt,
    save_fetched_job,
)

logger = logging.getLogger(__name__)


def _inbox() -> GmailInbox:
    return GmailInbox(
        config.HELLOWORK_IMAP_HOST,
        config.HELLOWORK_IMAP_PORT,
        config.HELLOWORK_IMAP_USERNAME,
        config.HELLOWORK_IMAP_APP_PASSWORD,
        config.HELLOWORK_IMAP_MAILBOX,
    )


async def _notify(bot, text: str) -> None:
    await bot.send_message(chat_id=config.YOUR_CHAT_ID, text=text)


async def ingest_email_once(bot, inbox: GmailInbox | None = None) -> int:
    inbox = inbox or _inbox()
    uid_validity, messages = await asyncio.to_thread(inbox.unread)
    handled = 0
    rejected: dict[str, int] = {}
    mailbox_key = config.HELLOWORK_IMAP_USERNAME.casefold()
    for item in messages:
        parsed = BytesParser(policy=policy.default).parsebytes(item.raw)
        message_id = str(parsed.get("Message-ID", ""))
        try:
            alert = parse_hellowork_alert(
                item.raw,
                allowed_sender_domain=config.HELLOWORK_EMAIL_ALLOWED_SENDER_DOMAIN,
            )
            offers = await resolve_alert_offers(alert)
            if not offers:
                raise HelloWorkEmailError("no valid HelloWork offer links found")
            inserted, duplicates, already = record_email_offers(
                config.JOBS_DB_PATH,
                mailbox_key=mailbox_key,
                uid_validity=uid_validity,
                uid=item.uid,
                message_id=alert.message_id or message_id,
                raw_message=item.raw,
                offers=offers,
            )
            await asyncio.to_thread(inbox.mark_seen, item.uid)
            handled += 1
            if not already:
                await _notify(
                    bot,
                    f"HelloWork email: {len(offers)} offers found, "
                    f"{inserted} queued, {duplicates} duplicates.",
                )
        except HelloWorkEmailError as exc:
            is_permanent = any(
                marker in str(exc).casefold()
                for marker in (
                    "sender", "dkim", "notification type", "message exceeds",
                    "no valid", "no hellowork tracking",
                )
            )
            if not is_permanent:
                logger.warning("HelloWork email intake will retry uid=%s: %s", item.uid, exc)
                continue
            inserted = record_rejected_email(
                config.JOBS_DB_PATH,
                mailbox_key=mailbox_key,
                uid_validity=uid_validity,
                uid=item.uid,
                message_id=message_id,
                raw_message=item.raw,
                reason=str(exc),
            )
            await asyncio.to_thread(inbox.mark_seen, item.uid)
            handled += 1
            if inserted:
                reason = str(exc)
                rejected[reason] = rejected.get(reason, 0) + 1
    if rejected:
        summary = ", ".join(
            f"{count} x {reason}" for reason, count in sorted(rejected.items())
        )
        await _notify(bot, f"HelloWork emails rejected: {summary}")
    return handled


async def _request_answers(bot, job_id: str, detail: str) -> None:
    fields = tuple(filter(None, (item.strip() for item in detail.split(","))))
    questions = tuple(
        FormQuestion(
            "hellowork", field, field, "text",
            canonical_fact=f"profile.answer.{field}", confidence=1.0,
        )
        for field in fields
    )
    if not questions:
        await _notify(bot, "HelloWork needs an unknown required answer; finish the application manually.")
        return
    batch_id = create_answer_batch(config.JOBS_DB_PATH, job_id, config.YOUR_CHAT_ID, questions)
    mark_job_awaiting_answers(config.JOBS_DB_PATH, job_id)
    rows = ["HelloWork needs application answers. Reply with one numbered answer per line.", ""]
    rows.extend(f"{index}. {question.label}" for index, question in enumerate(questions, 1))
    sent = await bot.send_message(chat_id=config.YOUR_CHAT_ID, text="\n".join(rows))
    message_id = int(getattr(sent, "message_id", 0) or 0)
    if message_id:
        set_batch_message_id(config.JOBS_DB_PATH, batch_id, message_id)


async def process_offer_once(bot) -> bool:
    queued = claim_next_offer(config.JOBS_DB_PATH)
    if not queued:
        return False
    offer_id = queued["offer_id"]
    url = queued["canonical_url"]
    job_id = ""
    try:
        page = await fetch_ats_page(url)
        draft = build_application_for_vacancy(page.vacancy, config.RESUME_DIR)
        preflight = await preflight_ats_application(
            url, draft.resume_path, config.APPLICATION_PROFILE_PATH,
            answer_db_path=config.JOBS_DB_PATH,
        )
        job_id = save_fetched_job(
            config.JOBS_DB_PATH, preflight.page, draft.direction,
            draft.resume_path.name, draft.message,
        )
        if preflight.questions:
            await _request_answers(
                bot, job_id, ",".join(question.field_id for question in preflight.questions)
            )
            finish_offer(config.JOBS_DB_PATH, offer_id, "paused", "answers_required")
            return True
        if not claim_job_for_send(config.JOBS_DB_PATH, job_id):
            finish_offer(config.JOBS_DB_PATH, offer_id, "completed")
            return True
        result = await submit_ats_application(
            url, draft.resume_path, config.APPLICATION_PROFILE_PATH,
            config.HELLOWORK_AUTH_STATE_PATH,
            headless=config.ATS_BROWSER_HEADLESS,
            answer_db_path=config.JOBS_DB_PATH,
        )
        record_send_attempt(
            config.JOBS_DB_PATH, job_id, "hellowork", url, result.status
        )
        if result.status == "submitted":
            mark_job_sent(config.JOBS_DB_PATH, job_id, result.url)
            finish_offer(config.JOBS_DB_PATH, offer_id, "completed")
            await _notify(bot, f"HelloWork application submitted: {page.vacancy.title}\n{result.url}")
        elif result.status == "answers_required":
            mark_job_send_failed(config.JOBS_DB_PATH, job_id)
            await _request_answers(bot, job_id, result.detail)
            finish_offer(config.JOBS_DB_PATH, offer_id, "paused", result.status)
        else:
            mark_job_send_failed(config.JOBS_DB_PATH, job_id)
            finish_offer(config.JOBS_DB_PATH, offer_id, "paused", result.status)
            await _notify(bot, f"HelloWork paused ({result.status}): {result.detail}\n{result.url}")
        return True
    except (AtsError, ResumeNotFoundError, UnknownDirectionError) as exc:
        if job_id:
            mark_job_send_failed(config.JOBS_DB_PATH, job_id)
        status = getattr(exc, "status", "failed")
        terminal = "paused" if status in {
            "auth_required", "requirements_unmet", "requirements_ambiguous",
            "resume_missing", "answers_required",
        } else "failed"
        finish_offer(config.JOBS_DB_PATH, offer_id, terminal, f"{type(exc).__name__}: {exc}")
        await _notify(bot, f"HelloWork offer {offer_id} {terminal}: {exc}")
        return True
    except Exception as exc:
        if job_id:
            mark_job_send_failed(config.JOBS_DB_PATH, job_id)
        finish_offer(config.JOBS_DB_PATH, offer_id, "failed", f"{type(exc).__name__}: {exc}")
        logger.exception("HelloWork offer processing failed offer_id=%s", offer_id)
        await _notify(bot, f"HelloWork offer {offer_id} failed safely: {type(exc).__name__}")
        return True


async def hellowork_email_worker(bot) -> None:
    if not config.HELLOWORK_IMAP_USERNAME or not config.HELLOWORK_IMAP_APP_PASSWORD:
        raise RuntimeError("HelloWork email ingestion is enabled but IMAP credentials are missing")
    while True:
        try:
            await ingest_email_once(bot)
            while await process_offer_once(bot):
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("HelloWork email worker cycle failed")
        await asyncio.sleep(config.HELLOWORK_IMAP_POLL_SECONDS)
