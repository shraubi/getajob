import logging
import pathlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import YOUR_CHAT_ID, SCORE_THRESHOLD
from bot.claude_client import score_vacancy, generate_application
from cv.renderer import render_cv_pdf
from storage.state import save_pending, get_pending, delete_pending

logger = logging.getLogger(__name__)

BASE_CV_PATH = pathlib.Path(__file__).parent.parent / "cv" / "base_cv.txt"


def _base_cv() -> str:
    return BASE_CV_PATH.read_text(encoding="utf-8")


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

    await _notify(ctx, "⏳ Смотрю вакансию...")

    try:
        score_data = await score_vacancy(jd)
    except Exception as e:
        await _notify(ctx, f"❌ Ошибка скоринга: {e}")
        return

    score = score_data.get("score", 0)
    reason = score_data.get("reason", "")
    role_title = score_data.get("role_title", "Developer")
    company = score_data.get("company", "Unknown")

    if score < SCORE_THRESHOLD:
        await _notify(
            ctx,
            f"⏭ *{company} — {role_title}*\nСкип ({score}/10)\n\n_{reason}_",
            parse_mode="Markdown",
        )
        return

    await _notify(
        ctx,
        f"✅ *{company} — {role_title}* ({score}/10)\n_{reason}_\n\nГенерирую...",
        parse_mode="Markdown",
    )

    try:
        result = await generate_application(jd, _base_cv())
    except Exception as e:
        await _notify(ctx, f"❌ Ошибка генерации: {e}")
        return

    cv_text = result.get("cv_text", _base_cv())
    tg_message = result.get("message", "")

    try:
        pdf_bytes = render_cv_pdf(cv_text, role_title)
    except Exception as e:
        await _notify(ctx, f"❌ Ошибка рендера PDF: {e}")
        return

    pdf_msg = await ctx.bot.send_document(
        chat_id=YOUR_CHAT_ID,
        document=pdf_bytes,
        filename=f"CV_{company.replace(' ', '_')}.pdf",
        caption=f"📄 CV для {company}",
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Отправить", callback_data=f"send:{pdf_msg.message_id}"),
        InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit:{pdf_msg.message_id}"),
        InlineKeyboardButton("❌ Пропустить", callback_data=f"skip:{pdf_msg.message_id}"),
    ]])

    await ctx.bot.send_message(
        chat_id=YOUR_CHAT_ID,
        text=f"💬 Сообщение для рекрутера:\n\n{tg_message}",
        reply_markup=keyboard,
    )

    save_pending(pdf_msg.message_id, {
        "cv_text": cv_text,
        "tg_message": tg_message,
        "role_title": role_title,
        "company": company,
        "jd": jd[:500],
    })


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, ref_id = query.data.split(":", 1)
    ref_id = int(ref_id)
    payload = get_pending(ref_id)

    if action == "skip":
        await query.edit_message_text("❌ Пропущено.")
        delete_pending(ref_id)

    elif action == "send":
        if not payload:
            await query.edit_message_text("⚠️ Данные не найдены, попробуй заново.")
            return
        await query.edit_message_text(
            f"✅ Скопируй и отправь рекрутеру вместе с PDF:\n\n{payload['tg_message']}",
        )
        delete_pending(ref_id)

    elif action == "edit":
        if not payload:
            await query.edit_message_text("⚠️ Данные не найдены, попробуй заново.")
            return
        await query.edit_message_text(
            f"✏️ Отредактируй и отправь мне обратно:\n\n{payload['tg_message']}"
        )
