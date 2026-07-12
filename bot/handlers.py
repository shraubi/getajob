import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
from storage.state import delete_pending, get_pending, save_pending
from job_page import JobPageError, extract_first_url, fetch_job_from_message
from token_free import (
    ResumeNotFoundError,
    UnknownDirectionError,
    build_application,
    build_application_for_vacancy,
)

logger = logging.getLogger(__name__)
_MIN_JD_LENGTH = 50


async def _notify(ctx, text: str, **kwargs):
    await ctx.bot.send_message(chat_id=config.YOUR_CHAT_ID, text=text, **kwargs)


async def _handle_token_free(ctx, text: str) -> None:
    parsed_page = None
    try:
        extract_first_url(text)
    except JobPageError:
        pass
    else:
        await _notify(ctx, "Fetching and parsing the linked job page...")
        try:
            parsed_page = await fetch_job_from_message(text)
        except JobPageError as exc:
            logger.warning("Job page parsing failed: %s", exc)
            await _notify(ctx, f"Could not parse the linked job page: {exc}")
            return

    try:
        draft = (
            build_application_for_vacancy(parsed_page.vacancy, config.RESUME_DIR)
            if parsed_page
            else build_application(text, config.RESUME_DIR)
        )
    except UnknownDirectionError:
        await _notify(ctx, "I could not confidently classify the fetched job.")
        return
    except ResumeNotFoundError as exc:
        logger.warning("Token-free resume missing: %s", exc)
        await _notify(ctx, f"{exc}\nUpload PDF resumes to the VM resume directory.")
        return

    summary = []
    if parsed_page:
        summary.extend((
            f"Source category: {parsed_page.source_category}",
            f"Page: {parsed_page.fetched_url}",
        ))
        if parsed_page.apply_url:
            summary.append(f"Apply/contact: {parsed_page.apply_url}")
    summary.extend((
        f"Direction: {draft.direction}",
        f"Role: {draft.vacancy.title}",
        f"Company: {draft.vacancy.company}",
    ))
    await _notify(ctx, "\n".join(summary))
    with draft.resume_path.open("rb") as resume:
        await ctx.bot.send_document(
            chat_id=config.YOUR_CHAT_ID,
            document=resume,
            filename=draft.resume_path.name,
            caption=f"Selected resume: {draft.direction}",
        )
    await _notify(ctx, f"Recruiter message:\n\n{draft.message}")

async def handle_vacancy_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.id != config.YOUR_CHAT_ID:
        return
    text = msg.text or msg.caption or ""
    if not text:
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
        chat_id=config.YOUR_CHAT_ID,
        document=pdf_bytes,
        filename=f"CV_{result.company.replace(' ', '_')}.pdf",
        caption=f"CV for {result.company}",
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Send", callback_data=f"send:{pdf_msg.message_id}"),
        InlineKeyboardButton("Edit", callback_data=f"edit:{pdf_msg.message_id}"),
        InlineKeyboardButton("Skip", callback_data=f"skip:{pdf_msg.message_id}"),
    ]])
    await ctx.bot.send_message(
        chat_id=config.YOUR_CHAT_ID,
        text=f"Recruiter message:\n\n{result.message}",
        reply_markup=keyboard,
    )
    save_pending(pdf_msg.message_id, {
        "cv_text": result.cv_text, "tg_message": result.message,
        "role_title": result.role_title, "company": result.company,
        "jd": jd[:500], "score": result.score,
    })


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if config.TOKEN_FREE_MODE:
        await query.edit_message_text("This legacy action expired after the token-free deployment.")
        return
    try:
        action, ref_id_str = query.data.split(":", 1)
        ref_id = int(ref_id_str)
    except (ValueError, AttributeError):
        await query.edit_message_text("Invalid action - please try again.")
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
