import hashlib
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
from bot.parser import parse_vacancy
from storage.database import JobStore
from storage.state import delete_pending, get_pending, save_pending
from token_free import ResumeNotFoundError, UnknownDirectionError, build_application

logger = logging.getLogger(__name__)
_MIN_JD_LENGTH = 50
_store = JobStore(config.DATABASE_PATH)


async def _notify(ctx, text: str, **kwargs):
    await ctx.bot.send_message(chat_id=config.YOUR_CHAT_ID, text=text, **kwargs)


def _suggested_response(title: str, company: str) -> str:
    return (
        f"Hi {company} team, I'm interested in the {title} role. "
        "My background looks relevant to the position, and I'd be happy to share my CV "
        "and discuss the role. Best regards"
    )


async def _handle_job_url(ctx, message, url: str) -> None:
    status = await message.reply_text("Parsing the job page…")
    try:
        vacancy = await parse_vacancy(url)
        job_id = _store.save_job(vacancy)
    except Exception as exc:
        logger.exception("Vacancy parsing failed")
        await status.edit_text(f"I couldn't parse that job page: {exc}")
        return

    response = _suggested_response(vacancy.title, vacancy.company)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm response", callback_data=f"respond_job:{job_id}"),
        InlineKeyboardButton("Skip", callback_data=f"skip_job:{job_id}"),
    ]])
    await status.edit_text(
        f"Parsed job #{job_id}\n\n"
        f"Title: {vacancy.title}\nCompany: {vacancy.company}\n"
        f"Source category: {vacancy.source.value}\nApply: {vacancy.application_url}\n\n"
        f"Suggested response:\n{response}",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def _handle_token_free(ctx, text: str) -> None:
    try:
        draft = build_application(text, config.RESUME_DIR)
    except UnknownDirectionError:
        await _notify(ctx, "I could not confidently choose a resume. Add a clearer role title or description.")
        return
    except ResumeNotFoundError as exc:
        logger.warning("Token-free resume missing: %s", exc)
        await _notify(ctx, f"{exc}\nUpload PDF resumes to the VM resume directory.")
        return

    await _notify(ctx, f"Direction: {draft.direction}\nRole: {draft.vacancy.title}\nCompany: {draft.vacancy.company}")
    with draft.resume_path.open("rb") as resume:
        await ctx.bot.send_document(
            chat_id=config.YOUR_CHAT_ID, document=resume,
            filename=draft.resume_path.name, caption=f"Selected resume: {draft.direction}",
        )
    await _notify(ctx, f"Recruiter message:\n\n{draft.message}")


async def handle_vacancy_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.id != config.YOUR_CHAT_ID:
        return
    text = msg.text or msg.caption or ""
    if not text:
        return
    match = re.search(r"https?://[^\s<>]+", text)
    if match:
        await _handle_job_url(ctx, msg, match.group(0).rstrip(".,);]"))
        return
    jd = text[:3000].strip()
    if len(jd) < _MIN_JD_LENGTH:
        await _notify(ctx, f"Too short to be a job description ({len(jd)} chars). Paste the full JD.")
        return
    if config.TOKEN_FREE_MODE:
        await _handle_token_free(ctx, jd)
        return

    from cv.renderer import render_cv_pdf
    from pipeline import run as pipeline_run

    async def on_progress(status: str) -> None:
        await _notify(ctx, status)

    try:
        result = await pipeline_run(jd, on_progress=on_progress)
    except Exception as exc:
        logger.exception("Pipeline error")
        await _notify(ctx, f"Error: {exc}")
        return
    if result.action == "skip":
        await _notify(ctx, f"Skip {result.company} - {result.role_title} ({result.score}/10)\n{result.reason}")
        return
    await _notify(ctx, f"{result.company} - {result.role_title} ({result.score}/10)\n{result.reason}")
    try:
        pdf_bytes = render_cv_pdf(result.cv_text, result.role_title)
    except Exception as exc:
        await _notify(ctx, f"PDF render error: {exc}")
        return
    pdf_msg = await ctx.bot.send_document(
        chat_id=config.YOUR_CHAT_ID, document=pdf_bytes,
        filename=f"CV_{result.company.replace(' ', '_')}.pdf", caption=f"CV for {result.company}",
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Send", callback_data=f"send:{pdf_msg.message_id}"),
        InlineKeyboardButton("Edit", callback_data=f"edit:{pdf_msg.message_id}"),
        InlineKeyboardButton("Skip", callback_data=f"skip:{pdf_msg.message_id}"),
    ]])
    await ctx.bot.send_message(
        chat_id=config.YOUR_CHAT_ID,
        text=f"Recruiter message:\n\n{result.message}", reply_markup=keyboard,
    )
    save_pending(pdf_msg.message_id, {
        "cv_text": result.cv_text, "tg_message": result.message,
        "role_title": result.role_title, "company": result.company,
        "jd": jd[:500], "score": result.score,
    })


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or query.from_user.id != config.YOUR_CHAT_ID:
        return
    await query.answer()
    try:
        action, ref_id_str = query.data.split(":", 1)
        ref_id = int(ref_id_str)
    except (ValueError, AttributeError):
        await query.edit_message_text("Invalid action - please try again.")
        return

    if action in {"respond_job", "skip_job"}:
        if action == "skip_job":
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Skipped.")
            return
        vacancy = _store.get_job(ref_id)
        if not vacancy:
            await query.message.reply_text("That saved job no longer exists.")
            return
        key = hashlib.sha256(f"telegram-response:{query.from_user.id}:{ref_id}".encode()).hexdigest()
        if not _store.begin_action(key, ref_id):
            await query.message.reply_text("Already confirmed — duplicate callback ignored.")
            return
        sent = await query.message.reply_text(_suggested_response(vacancy.title, vacancy.company))
        evidence = f"telegram_message_id:{sent.message_id}"
        _store.finish_action(key, evidence)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"Confirmed and recorded ({evidence}).")
        return

    if config.TOKEN_FREE_MODE:
        await query.edit_message_text("This legacy action expired after the token-free deployment.")
        return
    payload = get_pending(ref_id)
    if action == "skip":
        await query.edit_message_text("Skipped.")
        delete_pending(ref_id)
    elif action == "send":
        if not payload:
            await query.edit_message_text("Data not found. Please try again.")
            return
        from rag import store as rag_store
        rag_store.save_application(
            jd=payload.get("jd", ""), cv=payload.get("cv_text", ""),
            message=payload.get("tg_message", ""), score=payload.get("score", 0),
            role=payload.get("role_title", ""), company=payload.get("company", ""),
        )
        await query.edit_message_text(f"Copy and send with the PDF:\n\n{payload['tg_message']}")
        delete_pending(ref_id)
    elif action == "edit":
        if not payload:
            await query.edit_message_text("Data not found. Please try again.")
            return
        await query.edit_message_text(f"Edit and send back to me:\n\n{payload['tg_message']}")
