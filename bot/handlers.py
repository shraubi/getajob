import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import YOUR_CHAT_ID, SCORE_THRESHOLD
from pipeline import run as pipeline_run
from cv.renderer import render_cv_pdf
from storage.state import save_pending, get_pending, delete_pending
from rag import store as rag_store

logger = logging.getLogger(__name__)


async def _notify(ctx, text: str, **kwargs):
    await ctx.bot.send_message(chat_id=YOUR_CHAT_ID, text=text, **kwargs)


async def handle_vacancy_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.id != YOUR_CHAT_ID:
        return

    text = msg.text or msg.caption or ""
    if not text:
        return

    jd = text[:3000].strip()
    await _notify(ctx, "⏳ Reviewing vacancy...")

    try:
        result = await pipeline_run(jd)
    except Exception as e:
        logger.exception("Pipeline error")
        await _notify(ctx, f"❌ Error: {e}")
        return

    if result.action == "skip":
        await _notify(
            ctx,
            f"⏭ *{result.company} — {result.role_title}*\nSkip ({result.score}/10)\n\n_{result.reason}_",
            parse_mode="Markdown",
        )
        return

    await _notify(
        ctx,
        f"✅ *{result.company} — {result.role_title}* ({result.score}/10)\n_{result.reason}_",
        parse_mode="Markdown",
    )

    try:
        pdf_bytes = render_cv_pdf(result.cv_text, result.role_title)
    except Exception as e:
        await _notify(ctx, f"❌ PDF render error: {e}")
        return

    pdf_msg = await ctx.bot.send_document(
        chat_id=YOUR_CHAT_ID,
        document=pdf_bytes,
        filename=f"CV_{result.company.replace(' ', '_')}.pdf",
        caption=f"📄 CV for {result.company}",
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Send", callback_data=f"send:{pdf_msg.message_id}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{pdf_msg.message_id}"),
        InlineKeyboardButton("❌ Skip", callback_data=f"skip:{pdf_msg.message_id}"),
    ]])

    await ctx.bot.send_message(
        chat_id=YOUR_CHAT_ID,
        text=f"💬 Recruiter message:\n\n{result.message}",
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

    action, ref_id = query.data.split(":", 1)
    ref_id = int(ref_id)
    payload = get_pending(ref_id)

    if action == "skip":
        await query.edit_message_text("❌ Skipped.")
        delete_pending(ref_id)

    elif action == "send":
        if not payload:
            await query.edit_message_text("⚠️ Data not found, please try again.")
            return
        rag_store.save_application(
            jd=payload.get("jd", ""),
            cv=payload.get("cv_text", ""),
            message=payload.get("tg_message", ""),
            score=payload.get("score", 0),
            role=payload.get("role_title", ""),
            company=payload.get("company", ""),
        )
        await query.edit_message_text(
            f"✅ Copy and send to recruiter along with the PDF:\n\n{payload['tg_message']}",
        )
        delete_pending(ref_id)

    elif action == "edit":
        if not payload:
            await query.edit_message_text("⚠️ Data not found, please try again.")
            return
        await query.edit_message_text(
            f"✏️ Edit and send back to me:\n\n{payload['tg_message']}"
        )
