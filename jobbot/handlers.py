import logging
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from jobbot import config
from jobbot.classifier import score_directions
from jobbot.review_events import record_review_event
from jobbot.integrations.ats import (
    AtsError,
    fetch_ats_page,
    is_ats_job_url,
    preflight_ats_application,
    submit_ats_application,
)
from jobbot.integrations.hirify import HirifyError, HirifyClient, is_hirify_job_url
from jobbot.integrations.job_page import JobPageError, extract_first_url, fetch_job_from_message, resolve_application_url
from jobbot.store import (
    claim_job_for_send,
    enqueue_telegram_job,
    get_job,
    get_job_by_prefix,
    mark_job_send_failed,
    mark_job_awaiting_answers,
    mark_job_sent,
    record_send_attempt,
    save_fetched_job,
)
from jobbot.form_answers import (
    FormQuestion,
    close_batch,
    create_answer_batch,
    deduplicate_questions,
    fact_token,
    forget_fact_by_token,
    get_pending_batches,
    mark_batch_consented,
    parse_numbered_answers,
    save_batch_answers,
    set_batch_message_id,
)
from jobbot.application import (
    ResumeNotFoundError,
    UnknownDirectionError,
    build_application,
    build_application_for_vacancy,
    parse_vacancy,
    render_telegram_message,
)
from jobbot.integrations.telegram_input import telegram_message_url
from jobbot.integrations.web_application import (
    WebApplicationError,
    preflight_application,
    submit_application,
)

logger = logging.getLogger(__name__)
_MIN_JD_LENGTH = 50
_hirify_client: HirifyClient | None = None


def _get_hirify_client() -> HirifyClient:
    global _hirify_client
    if _hirify_client is None:
        _hirify_client = HirifyClient(config.HIRIFY_EMAIL, config.HIRIFY_PASSWORD)
    return _hirify_client


def _contact_page_from_apply_url(page):
    if not page or not page.apply_url:
        return page
    parsed = urlparse(page.apply_url)
    if parsed.scheme.casefold() == "mailto":
        address = unquote(parsed.path).strip()
        if address:
            return replace(
                page,
                source_category="email_contact",
                apply_url="",
                contact_kind="email",
                contact_value=address,
            )
    if (parsed.hostname or "").casefold() in {"t.me", "www.t.me"}:
        username = parsed.path.strip("/")
        if username:
            return replace(
                page,
                source_category="telegram_contact",
                apply_url=page.apply_url,
                contact_kind="telegram",
                contact_value=username,
            )
    return page


def _target_chat_id(ctx) -> int:
    return int(getattr(ctx, "_chat_id", None) or config.YOUR_CHAT_ID)


async def _notify(ctx, text: str, **kwargs):
    return await ctx.bot.send_message(chat_id=_target_chat_id(ctx), text=text, **kwargs)


def _format_question_batch(questions: tuple[FormQuestion, ...]) -> str:
    rows = [
        "I need a few application answers.",
        "Reply with one numbered answer per line. A valid reply authorizes immediate submission for this job.",
        "",
    ]
    for ordinal, question in enumerate(questions, 1):
        optional = " (optional; reply Skip)" if not question.required else ""
        rows.append(f"{ordinal}. {question.label}{optional}")
        if question.is_boolean:
            rows.append("   Allowed: Yes / No")
        elif question.options:
            rows.append(
                "   Options: " + " | ".join(
                    f"{index}. {option}" for index, option in enumerate(question.options, 1)
                )
            )
    return "\n".join(rows)


async def _send_question_batch(
    ctx,
    job_id: str,
    questions: tuple[FormQuestion, ...],
    *,
    chat_id: int | None = None,
) -> str:
    questions = deduplicate_questions(questions)
    target_chat = int(chat_id or _target_chat_id(ctx))
    batch_id = create_answer_batch(config.JOBS_DB_PATH, job_id, target_chat, questions)
    mark_job_awaiting_answers(config.JOBS_DB_PATH, job_id)
    sent = await ctx.bot.send_message(
        chat_id=target_chat,
        text=_format_question_batch(questions),
    )
    message_id = int(getattr(sent, "message_id", 0) or 0)
    if message_id:
        set_batch_message_id(config.JOBS_DB_PATH, batch_id, message_id)
    return batch_id


def _review_event(interaction_id: str, event_type: str, **data: object) -> None:
    if interaction_id:
        record_review_event(config.JOBS_DB_PATH, interaction_id, event_type, **data)

async def _handle_token_free(
    ctx, text: str, message_url: str = "", interaction_id: str = ""
) -> None:
    started = time.monotonic()
    parsed_page = None
    try:
        source_url = message_url or extract_first_url(text)
    except JobPageError:
        source_url = ""
    ats_preflight = None
    if source_url:
        await _notify(ctx, "Fetching and parsing the linked job page...")
        try:
            if is_ats_job_url(source_url):
                parsed_page = await fetch_ats_page(source_url)
            else:
                parsed_page = await fetch_job_from_message(source_url)
                if is_ats_job_url(parsed_page.fetched_url):
                    parsed_page = await fetch_ats_page(parsed_page.fetched_url)
            if is_hirify_job_url(parsed_page.fetched_url):
                contact = await _get_hirify_client().get_contact(parsed_page.fetched_url)
                if contact:
                    vacancy = parsed_page.vacancy
                    if vacancy.company == "Unknown company" and contact.company_title:
                        vacancy = replace(vacancy, company=contact.company_title)
                    apply_url = contact.target_url
                    if contact.kind == "url":
                        apply_url = await resolve_application_url(apply_url)
                    if is_ats_job_url(apply_url):
                        ats_page = await fetch_ats_page(apply_url)
                        parsed_page = replace(ats_page, vacancy=vacancy)
                    else:
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
            if parsed_page.apply_url and is_ats_job_url(parsed_page.apply_url):
                source_vacancy = parsed_page.vacancy
                ats_page = await fetch_ats_page(parsed_page.apply_url)
                parsed_page = replace(ats_page, vacancy=source_vacancy)
            parsed_page = _contact_page_from_apply_url(parsed_page)
        except (JobPageError, HirifyError, AtsError) as exc:
            logger.warning("Linked job processing failed: %s", exc)
            await _notify(ctx, f"Could not process the linked job: {exc}")
            _review_event(
                interaction_id, "job_fetch_failed", source_url=source_url,
                blocker_type=type(exc).__name__,
                expired=("404" in str(exc) or "no longer available" in str(exc).casefold()),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return

    try:
        draft = build_application_for_vacancy(parsed_page.vacancy, config.RESUME_DIR) if parsed_page else build_application(text, config.RESUME_DIR)
    except UnknownDirectionError:
        vacancy = parsed_page.vacancy if parsed_page else parse_vacancy(text)
        await _notify(ctx, "This role does not match any of the available resumes, so nothing will be sent.")
        _review_event(
            interaction_id, "role_rejected", source_url=source_url,
            title=vacancy.title, scores=score_directions(vacancy.title, vacancy.description),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return
    except ResumeNotFoundError as exc:
        logger.warning("Token-free resume missing: %s", exc)
        await _notify(ctx, f"{exc}\nUpload PDF resumes to the VM resume directory.")
        vacancy = parsed_page.vacancy if parsed_page else parse_vacancy(text)
        _review_event(
            interaction_id, "resume_missing", source_url=source_url,
            title=vacancy.title, direction="unknown",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return

    if parsed_page and parsed_page.contact_kind == "telegram":
        draft = replace(draft, message=render_telegram_message(parsed_page.fetched_url))
    if parsed_page and parsed_page.contact_kind == "ats":
        try:
            ats_preflight = await preflight_ats_application(
                parsed_page.fetched_url,
                draft.resume_path,
                config.APPLICATION_PROFILE_PATH,
                answer_db_path=config.JOBS_DB_PATH,
            )
        except AtsError as exc:
            await _notify(ctx, f"Could not prepare the ATS application: {exc}")
            _review_event(
                interaction_id, "application_failed", source_url=source_url,
                blocker_type=type(exc).__name__,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return

    job_id = save_fetched_job(
        config.JOBS_DB_PATH, parsed_page, draft.direction, draft.resume_path.name, draft.message
    ) if parsed_page else ""
    web_preflight = None
    if (
        parsed_page
        and parsed_page.apply_url
        and parsed_page.contact_kind == "web"
    ):
        try:
            web_preflight = await preflight_application(
                parsed_page.apply_url,
                draft.resume_path,
                config.APPLICATION_PROFILE_PATH,
                config.JOBS_DB_PATH,
                job_id=job_id,
                company=draft.vacancy.company,
            )
        except WebApplicationError as exc:
            await _notify(ctx, f"Could not prepare the web application: {exc}")
            _review_event(
                interaction_id, "application_failed", source_url=source_url,
                blocker_type=type(exc).__name__,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return
    logger.info(
        "Parsed job id=%s source=%s direction=%s title=%r resume=%s",
        job_id, parsed_page.source_category if parsed_page else "telegram_message", draft.direction,
        draft.vacancy.title, draft.resume_path.name,
    )
    summary = []
    if parsed_page:
        if parsed_page.contact_kind == "telegram":
            summary.append(f"Contact: @{parsed_page.contact_value.lstrip('@')}")
        elif parsed_page.contact_kind == "email":
            summary.append(f"Contact: {parsed_page.contact_value}")
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
    pending_questions = ()
    reused_questions = ()
    if ats_preflight:
        pending_questions = ats_preflight.questions
        reused_questions = ats_preflight.reused
    elif web_preflight:
        pending_questions = web_preflight.questions
        reused_questions = web_preflight.reused
    if pending_questions:
        await _send_question_batch(ctx, job_id, pending_questions)
        _review_event(
            interaction_id, "application_questions_requested", source_url=source_url,
            blocker_type="required_fields",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return

    confirmation = None
    if parsed_page and parsed_page.contact_kind == "ats":
        provider = parsed_page.contact_value.title()
        confirmation = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"Apply through {provider}", callback_data=f"atsapply:{job_id[:24]}"),
            InlineKeyboardButton("Skip", callback_data=f"applyskip:{job_id[:24]}"),
        ]])
    elif parsed_page and parsed_page.contact_kind == "email":
        confirmation = InlineKeyboardMarkup([[
            InlineKeyboardButton("Email recruiter", url=f"mailto:{parsed_page.contact_value}"),
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
    if reused_questions:
        unique_reused = deduplicate_questions(reused_questions)
        preview += "\n\nReusing saved answers:\n" + "\n".join(
            f"• {question.label}" for question in unique_reused
        )
        if confirmation:
            rows = [list(row) for row in confirmation.inline_keyboard]
            rows.extend(
                [InlineKeyboardButton(
                    f"Forget: {question.label[:34]}",
                    callback_data=f"forget:{fact_token(question)}",
                )]
                for question in unique_reused
            )
            confirmation = InlineKeyboardMarkup(rows)
    await _notify(ctx, preview, reply_markup=confirmation)
    _review_event(
        interaction_id, "job_previewed", source_url=source_url,
        job_id=job_id, title=draft.vacancy.title, direction=draft.direction,
        resume_preview=True, application_path=confirmation is not None,
        contact_kind=(parsed_page.contact_kind if parsed_page else ""),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def _preflight_saved_job(job: dict) -> tuple[FormQuestion, ...]:
    resume_path = config.RESUME_DIR / job["resume_name"]
    if job["contact_kind"] == "ats":
        preflight = await preflight_ats_application(
            job["page_url"], resume_path, config.APPLICATION_PROFILE_PATH,
            answer_db_path=config.JOBS_DB_PATH,
        )
        return preflight.questions
    preflight = await preflight_application(
        job["apply_url"], resume_path, config.APPLICATION_PROFILE_PATH,
        config.JOBS_DB_PATH, job_id=job["id"], company=job["company"],
    )
    return preflight.questions


def _forget_markup(questions: tuple[FormQuestion, ...]):
    rows = []
    for question in deduplicate_questions(questions):
        rows.append([InlineKeyboardButton(
            f"Forget: {question.label[:34]}",
            callback_data=f"forget:{fact_token(question)}",
        )])
    return InlineKeyboardMarkup(rows) if rows else None


async def _submit_answered_job(
    ctx,
    job: dict,
    batch_id: str,
    questions: tuple[FormQuestion, ...],
) -> None:
    if not mark_batch_consented(config.JOBS_DB_PATH, batch_id):
        await _notify(ctx, "This answer batch was already handled.")
        return
    if not claim_job_for_send(config.JOBS_DB_PATH, job["id"]):
        await _notify(ctx, "This application is already sending or sent.")
        return
    try:
        if job["contact_kind"] == "ats":
            provider = job["contact_value"]
            result = await submit_ats_application(
                job["page_url"],
                config.RESUME_DIR / job["resume_name"],
                config.APPLICATION_PROFILE_PATH,
                (
                    config.HELLOWORK_AUTH_STATE_PATH
                    if job["contact_value"] == "hellowork"
                    else config.ASHBY_BROWSER_PROFILE_PATH
                ),
                headless=config.ATS_BROWSER_HEADLESS,
                answer_db_path=config.JOBS_DB_PATH,
            )
            if result.status != "submitted":
                mark_job_send_failed(config.JOBS_DB_PATH, job["id"])
                record_send_attempt(
                    config.JOBS_DB_PATH, job["id"], provider,
                    job["page_url"], result.status,
                )
                await _notify(
                    ctx,
                    f"{provider.title()} needs your help: {result.detail}\nFinish here: {result.url}",
                )
                return
            mark_job_sent(config.JOBS_DB_PATH, job["id"], result.url)
            record_send_attempt(
                config.JOBS_DB_PATH, job["id"], provider, job["page_url"], "sent"
            )
            message = (
                f"Application submitted through {provider.title()} "
                f"with {job['resume_name']}.\n{result.url}"
            )
        else:
            result_url = await submit_application(
                job["apply_url"],
                config.RESUME_DIR / job["resume_name"],
                config.APPLICATION_PROFILE_PATH,
                job["recruiter_message"],
                answer_db_path=config.JOBS_DB_PATH,
                job_id=job["id"],
                company=job["company"],
            )
            mark_job_sent(config.JOBS_DB_PATH, job["id"], 0)
            message = f"Application submitted with {job['resume_name']}.\n{result_url}"
        await _notify(ctx, message, reply_markup=_forget_markup(questions))
    except Exception as exc:
        mark_job_send_failed(config.JOBS_DB_PATH, job["id"])
        logger.exception("Answered application failed for job %s", job["id"])
        await _notify(ctx, f"Application failed: {exc}\nFinish manually: {job['apply_url']}")


async def _try_handle_answer_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.message
    if not msg or msg.chat.id not in config.ALLOWED_CHAT_IDS:
        return False
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return False
    batches = get_pending_batches(config.JOBS_DB_PATH, msg.chat.id)
    if not batches:
        return False
    reply_id = int(
        getattr(getattr(msg, "reply_to_message", None), "message_id", 0) or 0
    )
    selected = next(
        (batch for batch in batches if reply_id and batch["bot_message_id"] == reply_id),
        None,
    )
    looks_numbered = bool(re.match(r"^\s*\d+\s*[.):-]", text))
    if selected is None:
        if not looks_numbered:
            return False
        if len(batches) != 1:
            await _notify(ctx, "Several applications are waiting. Reply to the specific question message.")
            return True
        selected = batches[0]
    questions = selected["questions"]
    parsed = parse_numbered_answers(text, questions)
    if parsed.answers:
        save_batch_answers(
            config.JOBS_DB_PATH, selected["id"], questions, parsed.answers
        )
    if parsed.errors:
        remaining = tuple(
            question for ordinal, question in enumerate(questions, 1)
            if ordinal not in parsed.answers
        )
        close_batch(config.JOBS_DB_PATH, selected["id"])
        await _notify(ctx, "Some answers need attention:\n" + "\n".join(parsed.errors))
        await _send_question_batch(ctx, selected["job_id"], remaining, chat_id=msg.chat.id)
        return True
    job = get_job(config.JOBS_DB_PATH, selected["job_id"])
    if not job:
        close_batch(config.JOBS_DB_PATH, selected["id"], "cancelled")
        await _notify(ctx, "The saved application was not found.")
        return True
    try:
        newly_missing = deduplicate_questions(await _preflight_saved_job(job))
    except (AtsError, WebApplicationError) as exc:
        close_batch(config.JOBS_DB_PATH, selected["id"], "cancelled")
        await _notify(ctx, f"Could not recheck the application: {exc}")
        return True
    if newly_missing:
        close_batch(config.JOBS_DB_PATH, selected["id"])
        await _send_question_batch(
            ctx, job["id"], newly_missing, chat_id=msg.chat.id
        )
        return True
    await _submit_answered_job(ctx, job, selected["id"], questions)
    return True


async def handle_vacancy_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.id not in config.ALLOWED_CHAT_IDS:
        return
    if await _try_handle_answer_message(update, ctx):
        return
    text = msg.text or msg.caption or ""
    if not text:
        return
    jd = text[:3000].strip()
    try:
        has_public_url = bool(extract_first_url(jd))
    except JobPageError:
        has_public_url = False
    if len(jd) < _MIN_JD_LENGTH and not has_public_url:
        await _notify(ctx, f"Too short to be a job description ({len(jd)} chars). Paste the full JD.")
        return
    await _handle_token_free(
        ctx, jd, telegram_message_url(msg),
        interaction_id=f"{msg.chat.id}:{msg.message_id}",
    )


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    query_message = getattr(query, "message", None)
    query_chat = getattr(getattr(query_message, "chat", None), "id", 0)
    query_message_id = getattr(query_message, "message_id", 0)
    if query_chat and query_chat not in config.ALLOWED_CHAT_IDS:
        await query.edit_message_text("This action is not authorized.")
        return
    interaction_id = f"callback:{query_chat}:{query_message_id}:{data.split(':', 1)[0]}"
    if data.startswith("applyskip:"):
        await query.edit_message_text("Application skipped.")
        return
    if data.startswith("forget:"):
        token = data.split(":", 1)[1]
        if forget_fact_by_token(config.JOBS_DB_PATH, token):
            await query.edit_message_text(
                "Saved answer forgotten. Submitted applications are unchanged."
            )
        else:
            await query.edit_message_text("That saved answer was already forgotten.")
        return
    if data.startswith(("atsapply:", "ashbyapply:")):
        prefix = data.split(":", 1)[1]
        job = get_job_by_prefix(config.JOBS_DB_PATH, prefix)
        if not job or job["contact_kind"] not in {"ats", "ashby"}:
            await query.edit_message_text("Saved ATS application was not found.")
            _review_event(interaction_id, "application_failed", blocker_type="missing_saved_job")
            return
        if not claim_job_for_send(config.JOBS_DB_PATH, job["id"]):
            await query.edit_message_text("This application is already sending or sent.")
            return
        provider = job["contact_value"] if job["contact_kind"] == "ats" else "ashby"
        try:
            result = await submit_ats_application(
                job["page_url"],
                config.RESUME_DIR / job["resume_name"],
                config.APPLICATION_PROFILE_PATH,
                (
                    config.HELLOWORK_AUTH_STATE_PATH
                    if job["contact_value"] == "hellowork"
                    else config.ASHBY_BROWSER_PROFILE_PATH
                ),
                headless=config.ATS_BROWSER_HEADLESS,
                answer_db_path=config.JOBS_DB_PATH,
            )
            if result.status == "submitted":
                mark_job_sent(config.JOBS_DB_PATH, job["id"], result.url)
                record_send_attempt(config.JOBS_DB_PATH, job["id"], provider, job["page_url"], "sent")
                await query.edit_message_text(
                    f"Application submitted through {provider.title()} with {job['resume_name']}.\n{result.url}"
                )
                _review_event(interaction_id, "application_sent", source_url=job["page_url"], channel=provider)
                return
            mark_job_send_failed(config.JOBS_DB_PATH, job["id"])
            record_send_attempt(config.JOBS_DB_PATH, job["id"], provider, job["page_url"], result.status)
            await query.edit_message_text(
                f"{provider.title()} needs your help: {result.detail}\nFinish here: {result.url}"
            )
            _review_event(interaction_id, "application_failed", source_url=job["page_url"], blocker_type=result.status)
            return
        except Exception as exc:
            mark_job_send_failed(config.JOBS_DB_PATH, job["id"])
            record_send_attempt(config.JOBS_DB_PATH, job["id"], provider, job["page_url"], "failed", exc)
            logger.exception("ATS application failed for job %s", job["id"])
            await query.edit_message_text(f"ATS application failed: {exc}\nFinish manually: {job['apply_url']}")
            _review_event(interaction_id, "application_failed", source_url=job["page_url"], blocker_type=type(exc).__name__)
            return
    if data.startswith("webapply:"):
        prefix = data.split(":", 1)[1]
        job = get_job_by_prefix(config.JOBS_DB_PATH, prefix)
        if not job or not job["apply_url"]:
            await query.edit_message_text("Saved web application was not found.")
            _review_event(interaction_id, "application_failed", blocker_type="missing_saved_job")
            return
        if not claim_job_for_send(config.JOBS_DB_PATH, job["id"]):
            await query.edit_message_text("This application is already sending or sent.")
            return
        try:
            submit_args = (
                job["apply_url"], config.RESUME_DIR / job["resume_name"],
                config.APPLICATION_PROFILE_PATH, job["recruiter_message"],
            )
            if job["contact_kind"] == "web":
                result_url = await submit_application(
                    *submit_args,
                    answer_db_path=config.JOBS_DB_PATH,
                    job_id=job["id"],
                    company=job["company"],
                )
            else:
                result_url = await submit_application(*submit_args)
            mark_job_sent(config.JOBS_DB_PATH, job["id"], 0)
        except Exception as exc:
            mark_job_send_failed(config.JOBS_DB_PATH, job["id"])
            logger.exception("Web application failed for job %s", job["id"])
            await query.edit_message_text(f"Application failed: {exc}")
            _review_event(interaction_id, "application_failed", source_url=job["page_url"], blocker_type=type(exc).__name__)
            return
        await query.edit_message_text(f"Application submitted with {job['resume_name']}.\n{result_url}")
        _review_event(interaction_id, "application_sent", source_url=job["page_url"], channel="web")
        return
    if data.startswith("hirifyapply:"):
        prefix = data.split(":", 1)[1]
        job = get_job_by_prefix(config.JOBS_DB_PATH, prefix)
        if not job or job["contact_kind"] != "hirify_direct":
            await query.edit_message_text("Saved Hirify application was not found.")
            _review_event(interaction_id, "application_failed", blocker_type="missing_saved_job")
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
            _review_event(interaction_id, "application_failed", source_url=job["page_url"], blocker_type=type(exc).__name__)
            return
        await query.edit_message_text(f"Applied through Hirify with {job['resume_name']} (application {external_id}).")
        _review_event(interaction_id, "application_sent", source_url=job["page_url"], channel="hirify")
        return
    if data.startswith("apply:"):
        prefix = data.split(":", 1)[1]
        job = get_job_by_prefix(config.JOBS_DB_PATH, prefix)
        if not job or job["contact_kind"] != "telegram":
            await query.edit_message_text("Saved Telegram application not found.")
            _review_event(interaction_id, "application_failed", blocker_type="missing_saved_job")
            return
        if not config.TELEGRAM_SENDING_ENABLED:
            await query.edit_message_text("Telegram sending is disabled; no message was sent.")
            _review_event(interaction_id, "telegram_throttled", source_url=job["page_url"], reason="sending_disabled", queue_present=False)
            return
        message = getattr(query, "message", None)
        chat = getattr(message, "chat", None)
        notify_chat_id = int(
            getattr(message, "chat_id", 0)
            or getattr(chat, "id", 0)
            or 0
        )
        queued = enqueue_telegram_job(
            config.JOBS_DB_PATH,
            job["id"],
            available_at=datetime.now(timezone.utc),
            reason="user_confirmed",
            interaction_id=interaction_id,
            notify_chat_id=notify_chat_id,
        )
        if not queued:
            await query.edit_message_text(
                "This Telegram application is already queued, sending, or sent."
            )
            return
        record_send_attempt(
            config.JOBS_DB_PATH,
            job["id"],
            "telegram",
            job["contact_value"],
            "queued",
        )
        await query.edit_message_text(
            f"Queued for Telegram delivery to @{job['contact_value'].lstrip('@')} "
            f"with {job['resume_name']}."
        )
        _review_event(
            interaction_id,
            "telegram_queued",
            source_url=job["page_url"],
            queue_present=True,
        )
        return
    await query.edit_message_text("Unknown action.")

