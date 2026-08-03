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
    close_stale_rejected_emails,
    finish_offer,
    mark_replay_unavailable,
    record_email_offers,
    record_rejected_email,
    replayable_rejected_uids,
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

_HELLOWORK_PARSER_REVISION = 2
_REPLAY_BATCH_SIZE = 25


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


def _diagnostic_text(values: dict[str, int]) -> str:
    return " ".join(f"{key}={values[key]}" for key in sorted(values))


async def ingest_email_once(bot, inbox: GmailInbox | None = None) -> int:
    inbox = inbox or _inbox()
    uid_validity, messages = await asyncio.to_thread(inbox.unread)
    handled = 0
    rejected: dict[str, int] = {}
    mailbox_key = config.HELLOWORK_IMAP_USERNAME.casefold()
    stale = close_stale_rejected_emails(
        config.JOBS_DB_PATH,
        mailbox_key=mailbox_key,
        current_uid_validity=uid_validity,
        parser_revision=_HELLOWORK_PARSER_REVISION,
    )
    if stale:
        logger.warning(
            "HelloWork replay closed stale receipts count=%s parser_revision=%s",
            stale, _HELLOWORK_PARSER_REVISION,
        )
    replay_uids = replayable_rejected_uids(
        config.JOBS_DB_PATH,
        mailbox_key=mailbox_key,
        uid_validity=uid_validity,
        parser_revision=_HELLOWORK_PARSER_REVISION,
        limit=_REPLAY_BATCH_SIZE,
    )
    unread_uids = {message.uid for message in messages}
    requested_replays = tuple(uid for uid in replay_uids if uid not in unread_uids)
    replay_messages = ()
    replay_message_uids: set[str] = set()
    if requested_replays:
        replay_validity, replay_messages, missing = await asyncio.to_thread(
            inbox.fetch_uids, requested_replays
        )
        if replay_validity != uid_validity:
            logger.warning(
                "HelloWork replay skipped because UIDVALIDITY changed requested=%s",
                len(requested_replays),
            )
            replay_messages = ()
            missing = requested_replays
        if missing:
            closed = mark_replay_unavailable(
                config.JOBS_DB_PATH,
                mailbox_key=mailbox_key,
                uid_validity=uid_validity,
                uids=missing,
                parser_revision=_HELLOWORK_PARSER_REVISION,
            )
            logger.warning(
                "HelloWork replay messages unavailable requested=%s closed=%s",
                len(missing), closed,
            )
        replay_message_uids = {message.uid for message in replay_messages}
    all_messages = (*messages, *replay_messages)
    logger.info(
        "HelloWork intake cycle uidvalidity=%s unread=%s replay=%s",
        uid_validity, len(messages), len(replay_messages),
    )
    for item in all_messages:
        parsed = BytesParser(policy=policy.default).parsebytes(item.raw)
        message_id = str(parsed.get("Message-ID", ""))
        try:
            alert = parse_hellowork_alert(item.raw)
            offers = await resolve_alert_offers(alert)
            if not offers:
                diagnostics = dict(alert.diagnostics)
                diagnostics["resolved_offers"] = 0
                raise HelloWorkEmailError(
                    "no valid HelloWork offer links found",
                    code="no_valid_offers",
                    permanent=True,
                    diagnostics=diagnostics,
                )
            inserted, duplicates, already = record_email_offers(
                config.JOBS_DB_PATH,
                mailbox_key=mailbox_key,
                uid_validity=uid_validity,
                uid=item.uid,
                message_id=alert.message_id or message_id,
                raw_message=item.raw,
                offers=offers,
                parser_revision=_HELLOWORK_PARSER_REVISION,
            )
            if item.uid not in replay_message_uids:
                await asyncio.to_thread(inbox.mark_seen, item.uid)
            handled += 1
            logger.info(
                "HelloWork email accepted uid=%s offers=%s inserted=%s duplicates=%s %s",
                item.uid, len(offers), inserted, duplicates,
                _diagnostic_text(alert.diagnostics),
            )
            if not already:
                await _notify(
                    bot,
                    f"HelloWork email: {len(offers)} offers found, "
                    f"{inserted} queued for role/resume screening, "
                    f"{duplicates} duplicates.",
                )
        except HelloWorkEmailError as exc:
            diagnostics = _diagnostic_text(exc.diagnostics)
            if not exc.permanent:
                logger.warning(
                    "HelloWork email intake will retry uid=%s code=%s reason=%s %s",
                    item.uid, exc.code, exc, diagnostics,
                )
                continue
            inserted = record_rejected_email(
                config.JOBS_DB_PATH,
                mailbox_key=mailbox_key,
                uid_validity=uid_validity,
                uid=item.uid,
                message_id=message_id,
                raw_message=item.raw,
                reason=str(exc),
                parser_revision=_HELLOWORK_PARSER_REVISION,
            )
            if item.uid not in replay_message_uids:
                await asyncio.to_thread(inbox.mark_seen, item.uid)
            handled += 1
            logger.warning(
                "HelloWork email rejected uid=%s code=%s reason=%s %s",
                item.uid, exc.code, exc, diagnostics,
            )
            if inserted:
                suffix = f" ({diagnostics})" if diagnostics else ""
                reason = f"[{exc.code}] {exc}{suffix}"
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
    except UnknownDirectionError:
        # A job alert can contain roles outside the configured resume
        # directions. That is an expected filtering outcome, not an
        # operational failure worth paging the user for every offer.
        finish_offer(config.JOBS_DB_PATH, offer_id, "skipped", "unsupported_vacancy")
        logger.info(
            "HelloWork offer skipped unsupported vacancy offer_id=%s",
            offer_id,
        )
        return True
    except (AtsError, ResumeNotFoundError) as exc:
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

