"""Background Gmail intake and automatic HelloWork application processing."""

from __future__ import annotations

import asyncio
import logging
from email import policy
from email.parser import BytesParser

from jobbot import config
from jobbot.email_store import (
    claim_next_offer,
    close_stale_rejected_emails,
    finish_offer,
    mark_replay_unavailable,
    record_email_offers,
    record_rejected_email,
    requeue_legacy_screened_offers,
    replayable_rejected_uids,
)
from jobbot.integrations.hellowork import (
    HelloWorkError,
    submit_hellowork_account_application,
)
from jobbot.integrations.hellowork_email import (
    GmailInbox,
    HelloWorkEmailError,
    parse_hellowork_alert,
    resolve_alert_offers,
)

logger = logging.getLogger(__name__)

_HELLOWORK_PARSER_REVISION = 2
_HELLOWORK_APPLICATION_REVISION = 4
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
                    f"{inserted} queued for direct account application, "
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


async def process_offer_once(bot=None, reports: list[dict[str, str]] | None = None) -> str | None:
    queued = claim_next_offer(config.JOBS_DB_PATH)
    if not queued:
        return None
    offer_id = queued["offer_id"]
    url = queued["canonical_url"]
    try:
        result = await submit_hellowork_account_application(
            url,
            config.HELLOWORK_AUTH_STATE_PATH,
            headless=config.ATS_BROWSER_HEADLESS,
            profile_path=config.APPLICATION_PROFILE_PATH,
            answer_db_path=config.JOBS_DB_PATH,
        )
        if result.status in {"submitted", "already_applied"}:
            finish_offer(
                config.JOBS_DB_PATH, offer_id, "completed", result.detail,
                application_revision=_HELLOWORK_APPLICATION_REVISION,
            )
        else:
            terminal = "failed" if result.status in {"failed", "unavailable"} else "paused"
            finish_offer(
                config.JOBS_DB_PATH, offer_id, terminal, result.status,
                application_revision=_HELLOWORK_APPLICATION_REVISION,
            )
        logger.info(
            "HelloWork direct account application offer_id=%s status=%s detail=%s",
            offer_id, result.status, result.detail,
        )
        if reports is not None:
            reports.append({
                "offer_id": offer_id,
                "status": result.status,
                "detail": result.detail,
                "url": url,
            })
        return result.status
    except HelloWorkError as exc:
        status = getattr(exc, "status", "failed")
        finish_offer(
            config.JOBS_DB_PATH, offer_id, "failed",
            f"{type(exc).__name__}: {exc}",
            application_revision=_HELLOWORK_APPLICATION_REVISION,
        )
        logger.warning(
            "HelloWork direct account application failed offer_id=%s status=%s error_type=%s",
            offer_id, status, type(exc).__name__,
        )
        if reports is not None:
            reports.append({
                "offer_id": offer_id,
                "status": status,
                "detail": f"error_type={type(exc).__name__}",
                "url": url,
            })
        return status
    except Exception as exc:
        finish_offer(
            config.JOBS_DB_PATH, offer_id, "failed",
            f"{type(exc).__name__}: {exc}",
            application_revision=_HELLOWORK_APPLICATION_REVISION,
        )
        logger.exception("HelloWork offer processing failed offer_id=%s", offer_id)
        if reports is not None:
            reports.append({
                "offer_id": offer_id,
                "status": "failed",
                "detail": f"error_type={type(exc).__name__}",
                "url": url,
            })
        return "failed"


async def process_pending_offers(bot) -> dict[str, int]:
    recovered = requeue_legacy_screened_offers(
        config.JOBS_DB_PATH,
        application_revision=_HELLOWORK_APPLICATION_REVISION,
    )
    if recovered:
        logger.info(
            "HelloWork requeued offers blocked by legacy screening count=%s",
            recovered,
        )
    outcomes: dict[str, int] = {}
    reports: list[dict[str, str]] = []
    while outcome := await process_offer_once(reports=reports):
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    if outcomes:
        summary = ", ".join(
            f"{count} {status}"
            for status, count in sorted(outcomes.items())
        )
        await _notify(bot, f"HelloWork applications: {summary}.")
        attention = [
            report for report in reports
            if report["status"] not in {"submitted", "already_applied"}
        ]
        if attention:
            lines = ["HelloWork attention needed:"]
            for report in attention:
                status = report["status"]
                if status == "confirmation_required":
                    action = "open this offer and finish the final confirmation"
                elif status == "submission_unknown":
                    action = "check HelloWork > Mes candidatures; if absent, open this offer"
                elif status == "unavailable":
                    action = "no Postuler control was available"
                elif status == "auth_required":
                    action = "HelloWork login or CAPTCHA needs attention"
                elif status == "answers_required":
                    action = "required fields could not be filled from the saved applicant profile"
                else:
                    action = "application failed; diagnostic follows"
                lines.append(
                    f'- offer {report["offer_id"]} [{status}]: {action}: '
                    f'{report["url"]} ({report["detail"]})'
                )
            await _notify(bot, "\n".join(lines))
    return outcomes


async def hellowork_email_worker(bot) -> None:
    failure_reported = False
    while True:
        try:
            if not config.HELLOWORK_IMAP_USERNAME or not config.HELLOWORK_IMAP_APP_PASSWORD:
                raise RuntimeError(
                    "HelloWork email ingestion is enabled but IMAP credentials are missing"
                )
            await ingest_email_once(bot)
            await process_pending_offers(bot)
            if failure_reported:
                await _notify(bot, "HelloWork email intake recovered.")
                failure_reported = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("HelloWork email worker cycle failed")
            if not failure_reported:
                try:
                    await _notify(
                        bot,
                        "HelloWork email intake stopped processing mail: "
                        f"{type(exc).__name__}. It will keep retrying; check the bot logs "
                        "and Gmail app-password settings.",
                    )
                except Exception:
                    logger.exception("Could not report HelloWork email worker failure")
                failure_reported = True
        await asyncio.sleep(config.HELLOWORK_IMAP_POLL_SECONDS)

