import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from jobbot import config
from jobbot.integrations.ashby import (
    AshbyError,
    fetch_ashby_posting,
    format_missing_questions,
    is_ashby_job_url,
    preflight_ashby_application,
    submit_ashby_application,
)
from jobbot.integrations.hirify import HirifyError, HirifyClient, is_hirify_job_url
from jobbot.integrations.job_page import JobPageError, extract_first_url, fetch_job_from_message, resolve_application_url
from jobbot.store import (
    claim_job_for_send,
    claim_telegram_job_for_send,
    get_job_by_prefix,
    mark_job_send_failed,
    mark_job_sent,
    record_send_attempt,
    save_fetched_job,
    set_sender_cooldown,
)
from jobbot.application import (
    ResumeNotFoundError,
    UnknownDirectionError,
    build_application,
    build_application_for_vacancy,
    render_telegram_message,
)
from jobbot.integrations.telegram_sender import TelegramPeerFloodError, TelegramSender, TelegramSenderError
from jobbot.integrations.telegram_input import telegram_message_url
from jobbot.integrations.web_application import WebApplicationError, submit_application

logger = logging.getLogger(__name__)
_MIN_JD_LENGTH = 50
_hirify_client: HirifyClient | None = None


def _get_hirify_client() -> HirifyClient:
    global _hirify_client
    if _hirify_client is None:
        _hirify_client = HirifyClient(config.HIRIFY_EMAIL, config.HIRIFY_PASSWORD)
    return _hirify_client


def _target_chat_id(ctx) -> int:
    return int(getattr(ctx, "_chat_id", None) or config.YOUR_CHAT_ID)


async def _notify(ctx, text: str, **kwargs):
    await ctx.bot.send_message(chat_id=_target_chat_id(ctx), text=text, **kwargs)


async def _handle_token_free(ctx, text: str, message_url: str = "") -> None:
    parsed_page = None
    try:
        source_url = message_url or extract_first_url(text)
    except JobPageError:
        source_url = ""
    ashby_preflight = None
    if source_url:
        await _notify(ctx, "Fetching and parsing the linked job page...")
        try:
            if is_ashby_job_url(source_url):
                ashby_posting = await fetch_ashby_posting(source_url)
                parsed_page = ashby_posting.page
            else:
                parsed_page = await fetch_job_from_message(source_url)
                if is_ashby_job_url(parsed_page.fetched_url):
                    ashby_posting = await fetch_ashby_posting(parsed_page.fetched_url)
                    parsed_page = ashby_posting.page
            if is_hirify_job_url(parsed_page.fetched_url):
                contact = await _get_hirify_client().get_contact(parsed_page.fetched_url)
                if contact:
                    vacancy = parsed_page.vacancy
                    if vacancy.company == "Unknown company" and contact.company_title:
                        vacancy = replace(vacancy, company=contact.company_title)
                    apply_url = contact.target_url
                    if contact.kind == "url":
                        apply_url = await resolve_application_url(apply_url)
                    parsed_page = replace(
                        parsed_page,
                        vacancy=vacancy,
                        source_category="telegram_contact" if contact.kind == "telegram" else "external_application_url",
                        apply_url=apply_url,
                        contact_kind=contact.kind,
                        contact_value=contact.value,
                    )
                else:
                    direct = await _get_hirify_client().get_direct_application(parsed_page.fetched_url)
                    if direct:
                        parsed_page = replace(
                            parsed_page,
                            source_category="hirify_direct_application",
                            apply_url=parsed_page.fetched_url,
                            contact_kind="hirify_direct",
                            contact_value=str(direct.vacancy_id),
                        )
        except (JobPageError, HirifyError, AshbyError) as exc:
            logger.warning("Linked job processing failed: %s", exc)
            await _notify(ctx, f"Could not process the linked job: {exc}")
            return

    try:
        draft = build_application_for_vacancy(parsed_page.vacancy, config.RESUME_DIR) if parsed_page else build_application(text, config.RESUME_DIR)
    except UnknownDirectionError:
        await _notify(ctx, "This role does not match any of the available resumes, so nothing will be sent.")
        return
    except ResumeNotFoundError as exc:
        logger.warning("Token-free resume missing: %s", exc)
        await _notify(ctx, f"{exc}\nUpload PDF resumes to the VM resume directory.")
        return

    if parsed_page and parsed_page.contact_kind == "telegram":
        draft = replace(draft, message=render_telegram_message(parsed_page.fetched_url))
    if parsed_page and parsed_page.contact_kind == "ashby":
        try:
            ashby_preflight = await preflight_ashby_application(
                parsed_page.fetched_url,
                draft.resume_path,
                config.APPLICATION_PROFILE_PATH,
            )
        except AshbyError as exc:
            await _notify(ctx, f"Could not prepare the Ashby application: {exc}")
            return

    job_id = save_fetched_job(
        config.JOBS_DB_PATH, parsed_page, draft.direction, draft.resume_path.name, draft.message
    ) if parsed_page else ""
    logger.info(
        "Parsed job id=%s source=%s direction=%s title=%r resume=%s",
        job_id, parsed_page.source_category if parsed_page else "telegram_message", draft.direction,
        draft.vacancy.title, draft.resume_path.name,
    )
    summary = []
    if parsed_page:
        if parsed_page.contact_kind == "telegram":
            summary.append(f"Contact: @{parsed_page.contact_value.lstrip('@')}")
        elif parsed_page.apply_url:
            summary.append(f"Apply: {parsed_page.apply_url}")
    summary.extend((f"Direction: {draft.direction}", f"Role: {draft.vacancy.title}", f"Company: {draft.vacancy.company}"))
    await _notify(ctx, "\n".join(summary))
    with draft.resume_path.open("rb") as resume:
        await ctx.bot.send_document(
            chat_id=_target_chat_id(ctx),
            document=resume,
            filename=draft.resume_path.name,
            caption=f"Selected resume: {draft.direction}",
        )
    if ashby_preflight and ashby_preflight.missing:
        await _notify(
            ctx,
            format_missing_questions(ashby_preflight)
            + "\nAdd these answers to applicant.json under \"answers\", then send the vacancy again.",
        )
        return

    confirmation = None
    if parsed_page and parsed_page.contact_kind == "ashby":
        confirmation = InlineKeyboardMarkup([[
            InlineKeyboardButton("Apply through Ashby", callback_data=f"ashbyapply:{job_id[:24]}"),
            InlineKeyboardButton("Skip", callback_data=f"applyskip:{job_id[:24]}"),
        ]])
    elif parsed_page and parsed_page.contact_kind == "telegram":
        confirmation = InlineKeyboardMarkup([[
            InlineKeyboardButton("Send to recruiter", callback_data=f"apply:{job_id[:24]}"),
            InlineKeyboardButton("Skip", callback_data=f"applyskip:{job_id[:24]}"),
        ]])
    elif parsed_page and parsed_page.contact_kind == "hirify_direct":
        confirmation = InlineKeyboardMarkup([[
            InlineKeyboardButton("Apply through Hirify", callback_data=f"hirifyapply:{job_id[:24]}"),
            InlineKeyboardButton("Skip", callback_data=f"applyskip:{job_id[:24]}"),
        ]])
    elif parsed_page and parsed_page.apply_url:
        confirmation = InlineKeyboardMarkup([[
            InlineKeyboardButton("Apply with resume", callback_data=f"webapply:{job_id[:24]}"),
            InlineKeyboardButton("Skip", callback_data=f"applyskip:{job_id[:24]}"),
        ]])
    preview = f"Recruiter message:\n\n{draft.message}" if draft.message else "No cover message â€” resume only."
    await _notify(ctx, preview, reply_markup=confirmation)


async def handle_vacancy_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.id not in config.ALLOWED_CHAT_IDS:
        return
    text = msg.text or msg.caption or ""
    if not text:
        return
    jd = text[:3000].strip()
    if len(jd) < _MIN_JD_LENGTH:
        await _notify(ctx, f"Too short to be a job description ({len(jd)} chars). Paste the full JD.")
        return
    await _handle_token_free(ctx, jd, telegram_message_url(msg))


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("applyskip:"):
        await query.edit_message_text("Application skipped.")
        return
    if data.startswith("ashbyapply:"):
        prefix = data.split(":", 1)[1]
        job = get_job_by_prefix(config.JOBS_DB_PATH, prefix)
        if not job or job["contact_kind"] != "ashby":
            await query.edit_message_text("Saved Ashby application was not found.")
            return
        if not claim_job_for_send(config.JOBS_DB_PATH, job["id"]):
            await query.edit_message_text("This application is already sending or sent.")
            return
        try:
            result = await submit_ashby_application(
                job["page_url"],
                config.RESUME_DIR / job["resume_name"],
                config.APPLICATION_PROFILE_PATH,
                config.ASHBY_BROWSER_PROFILE_PATH,
                headless=config.ASHBY_BROWSER_HEADLESS,
            )
            if result.status == "submitted":
                mark_job_sent(config.JOBS_DB_PATH, job["id"], result.url)
                record_send_attempt(
                    config.JOBS_DB_PATH, job["id"], "ashby", job["page_url"], "sent"
                )
                await query.edit_message_text(
                    f"Application submitted through Ashby with {job['resume_name']}.\n{result.url}"
                )
                return
            mark_job_send_failed(config.JOBS_DB_PATH, job["id"])
            record_send_attempt(
                config.JOBS_DB_PATH, job["id"], "ashby", job["page_url"], result.status
            )
            await query.edit_message_text(
                f"Ashby needs your help: {result.detail}\nFinish here: {result.url}"
            )
            return
        except Exception as exc:
            mark_job_send_failed(config.JOBS_DB_PATH, job["id"])
            record_send_attempt(
                config.JOBS_DB_PATH, job["id"], "ashby", job["page_url"], "failed", exc
            )
            logger.exception("Ashby application failed for job %s", job["id"])
            await query.edit_message_text(
                f"Ashby application failed: {exc}\nFinish manually: {job['apply_url']}"
            )
            return
    if data.startswith("webapply:"):
        prefix = data.split(":", 1)[1]
        job = get_job_by_prefix(config.JOBS_DB_PATH, prefix)
        if not job or not job["apply_url"]:
            await query.edit_message_text("Saved web application was not found.")
            return
        if not claim_job_for_send(config.JOBS_DB_PATH, job["id"]):
            await query.edit_message_text("This application is already sending or sent.")
            return
        try:
            result_url = await submit_application(
                job["apply_url"], config.RESUME_DIR / job["resume_name"],
                config.APPLICATION_PROFILE_PATH, job["recruiter_message"],
            )
            mark_job_sent(config.JOBS_DB_PATH, job["id"], 0)
        except Exception as exc:
            mark_job_send_failed(config.JOBS_DB_PATH, job["id"])
            logger.exception("Web application failed for job %s", job["id"])
            await query.edit_message_text(f"Application failed: {exc}")
            return
        await query.edit_message_text(f"Application submitted with {job['resume_name']}.\n{result_url}")
        return
    if data.startswith("hirifyapply:"):
        prefix = data.split(":", 1)[1]
        job = get_job_by_prefix(config.JOBS_DB_PATH, prefix)
        if not job or job["contact_kind"] != "hirify_direct":
            await query.edit_message_text("Saved Hirify application was not found.")
            return
        if not claim_job_for_send(config.JOBS_DB_PATH, job["id"]):
            await query.edit_message_text("This application is already sending or sent.")
            return
        try:
            external_id = await _get_hirify_client().apply_direct(int(job["contact_value"]))
            mark_job_sent(config.JOBS_DB_PATH, job["id"], external_id)
        except Exception as exc:
            mark_job_send_failed(config.JOBS_DB_PATH, job["id"])
            logger.exception("Hirify direct application failed for job %s", job["id"])
            await query.edit_message_text(f"Application failed: {exc}")
            return
        await query.edit_message_text(f"Applied through Hirify with {job['resume_name']} (application {external_id}).")
        return
    if data.startswith("apply:"):
        prefix = data.split(":", 1)[1]
        job = get_job_by_prefix(config.JOBS_DB_PATH, prefix)
        if not job or job["contact_kind"] != "telegram":
            await query.edit_message_text("Saved Telegram application not found.")
            return
        if not config.TELEGRAM_SENDING_ENABLED:
            await query.edit_message_text("Telegram sending is disabled; no message was sent.")
            return
        claimed, retry_at, reason = claim_telegram_job_for_send(
            config.JOBS_DB_PATH,
            job["id"],
            min_interval_seconds=config.TELEGRAM_SEND_MIN_INTERVAL_SECONDS,
            max_per_hour=config.TELEGRAM_SEND_MAX_PER_HOUR,
        )
        if not claimed:
            if retry_at:
                record_send_attempt(
                    config.JOBS_DB_PATH, job["id"], "telegram", job["contact_value"], "throttled"
                )
                await query.edit_message_text(
                    f"Telegram send paused until {retry_at.astimezone(timezone.utc):%Y-%m-%d %H:%M UTC}: {reason}."
                )
            else:
                await query.edit_message_text("This application is already sending or sent.")
            return
        try:
            if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
                raise TelegramSenderError("Telegram sender is not configured")
            sender = TelegramSender(config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH, config.TELEGRAM_SESSION_PATH)
            external_id = await sender.send_resume(
                job["contact_value"], job["recruiter_message"], config.RESUME_DIR / job["resume_name"]
            )
            mark_job_sent(config.JOBS_DB_PATH, job["id"], external_id)
            record_send_attempt(
                config.JOBS_DB_PATH, job["id"], "telegram", job["contact_value"], "sent"
            )
        except TelegramPeerFloodError as exc:
            blocked_until = datetime.now(timezone.utc) + timedelta(
                hours=config.TELEGRAM_PEER_FLOOD_COOLDOWN_HOURS
            )
            set_sender_cooldown(config.JOBS_DB_PATH, "telegram", blocked_until, "Telegram PeerFlood")
            mark_job_send_failed(config.JOBS_DB_PATH, job["id"])
            record_send_attempt(
                config.JOBS_DB_PATH, job["id"], "telegram", job["contact_value"], "peer_flood", exc
            )
            logger.warning("Telegram PeerFlood; sends paused until %s", blocked_until.isoformat())
            await query.edit_message_text(
                f"Telegram restricted outbound messages. Automatic sends are paused until "
                f"{blocked_until:%Y-%m-%d %H:%M UTC}; this application was not sent."
            )
            return
        except Exception as exc:
            mark_job_send_failed(config.JOBS_DB_PATH, job["id"])
            record_send_attempt(
                config.JOBS_DB_PATH, job["id"], "telegram", job["contact_value"], "failed", exc
            )
            logger.exception("Telegram application send failed for job %s", job["id"])
            await query.edit_message_text(f"Send failed: {exc}")
            return
        await query.edit_message_text(
            f"Sent to @{job['contact_value'].lstrip('@')} with {job['resume_name']} (message {external_id})."
        )
        return
    await query.edit_message_text("Unknown action.")
