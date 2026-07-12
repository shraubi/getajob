import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
from cv.renderer import render_cv_pdf
from pipeline import run as pipeline_run
from rag import store as rag_store
from storage.state import delete_pending, get_pending, save_pending
from token_free import ResumeNotFoundError, UnknownDirectionError, build_application

logger = logging.getLogger(__name__)

_MIN_JD_LENGTH = 50


async def _notify(ctx, text: str, **kwargs):
    await ctx.bot.send_message(chat_id=config.YOUR_CHAT_ID, text=text, **kwargs)


async def _handle_token_free(ctx, text: str) -> None:
    try:
        draft = build_application(text, config.RESUME_DIR, config.RESUME_FILES)
    except UnknownDirectionError:
        await _notify(
            ctx,
            "ðŸ¤· I couldn't confidently choose a rÃ©sumÃ©. Add a clearer role title/description "
            "or keep TOKEN_FREE_MODE disabled for this vacancy.",
        )
        return
    except ResumeNotFoundError as exc:
        logger.warning("Token-free resume missing: %s", exc)
        await _notify(ctx, f"ðŸ“ {exc}\nUpload it to the VM or update the RESUME_* setting.")
        return

    await _notify(
        ctx,
        f"âœ… Direction: {draft.direction}\n"
        f"Role: {draft.vacancy.title}\n"
        f"Company: {draft.vacancy.company}",
    )
    with draft.resume_path.open("rb") as resume:
        await ctx.bot.send_document(
            chat_id=config.YOUR_CHAT_ID,
            document=resume,
            filename=draft.resume_path.name,
            caption=f"ðŸ“„ Selected rÃ©sumÃ©: {draft.direction}",
        )
    await _notify(ctx, f"ðŸ’¬ Recruiter message:\n\n{draft.message}")


async def handle_vacancy_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.id != config.YOUR_CHAT_ID:
        return

    text = msg.text or msg.caption or ""
    if not text:
        return
    jd = text[:3000].strip()
    if len(jd) < _MIN_JD_LENGTH:
        await _notify(ctx, f"âš ï¸ Too short to be a job description ({len(jd)} chars). Paste the full JD.")
        return

    if config.TOKEN_FREE_MODE:
        await _handle_token_free(ctx, jd)
        return

    async def on_progress(status: str) -> None:
        await _notify(ctx, status)

    try:
        result = await pipeline_run(jd, on_progress=on_progress)
    except Exception as exc:
        logger.exception("Pipeline error")
        await _notify(ctx, f"âŒ Error: {exc}")
        return

    if result.action == "skip":
        await _notify(
            ctx,
            f"â­ *{result.company} â€” {result.role_title}*\nSkip ({result.score}/10)\n\n_{result.reason}_",
            parse_mode="Markdown",
        )
        return

    await _notify(
        ctx,
        f"âœ… *{result.company} â€” {result.role_title}* ({result.score}/10)\n_{result.reason}_",
        parse_mode="Markdown",
    )
    try:
        pdf_bytes = render_cv_pdf(result.cv_text, result.role_title)
    except Exception as exc:
        await _notify(ctx, f"âŒ PDF render error: {exc}")
        return

    pdf_msg = await ctx.bot.send_document(
        chat_id=config.YOUR_CHAT_ID,
        document=pdf_bytes,
        filename=f"CV_{result.company.replace(' ', '_')}.pdf",
        caption=f"ðŸ“„ CV for {result.company}",
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("âœ… Send", callback_data=f"send:{pdf_msg.message_id}"),
        InlineKeyboardButton("âœï¸ Edit", callback_data=f"edit:{pdf_msg.message_id}"),
        InlineKeyboardButton("âŒ Skip", callback_data=f"skip:{pdf_msg.message_id}"),
    ]])
    await ctx.bot.send_message(
        chat_id=config.YOUR_CHAT_ID,
        text=f"ðŸ’¬ Recruiter message:\n\n{result.message}",
        reply_markup=keyboard,
    )
    save_pending(pdf_msg.message_id, {
        "cv_text": result.cv_text,
        "tg_message": result.message,
        "role_title": result.role_title,
        "company": result.company,
        "jd": jd[:500],
        "score": result.score,
    })


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        action, ref_id_str = query.data.split(":", 1)
        ref_id = int(ref_id_str)
    except (ValueError, AttributeError):
        await query.edit_message_text("âš ï¸ Invalid action â€” please try again.")
        return

    payload = get_pending(ref_id)
    if action == "skip":
        await query.edit_message_text("âŒ Skipped.")
        delete_pending(ref_id)
    elif action == "send":
        if not payload:
            await query.edit_message_text("âš ï¸ Data not found â€” the bot may have restarted. Please try again.")
            return
        rag_store.save_application(
            jd=payload.get("jd", ""), cv=payload.get("cv_text", ""),
            message=payload.get("tg_message", ""), score=payload.get("score", 0),
            role=payload.get("role_title", ""), company=payload.get("company", ""),
        )
        await query.edit_message_text(
            f"âœ… Copy and send to recruiter along with the PDF:\n\n{payload['tg_message']}"
        )
        delete_pending(ref_id)
    elif action == "edit":
        if not payload:
            await query.edit_message_text("âš ï¸ Data not found â€” the bot may have restarted. Please try again.")
            return
        await query.edit_message_text(f"âœï¸ Edit and send back to me:\n\n{payload['tg_message']}")
