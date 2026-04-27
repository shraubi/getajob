import logging
import pathlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import YOUR_CHAT_ID, VACANCY_SOURCE_CHAT_ID, SCORE_THRESHOLD
from bot.parser import extract_jd
from bot.claude_client import score_vacancy, generate_application
from cv.renderer import render_cv_pdf
from storage.state import save_pending, get_pending, delete_pending

logger = logging.getLogger(__name__)

BASE_CV_PATH = pathlib.Path(__file__).parent.parent / "cv" / "base_cv.txt"


def _base_cv() -> str:
    return BASE_CV_PATH.read_text()


async def _notify(ctx, text: str, **kwargs):
    """Send a message to your personal chat."""
    await ctx.bot.send_message(chat_id=YOUR_CHAT_ID, text=text, **kwargs)


async def handle_vacancy_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg:
        return

    chat_id = msg.chat.id

    # Accept from: your personal chat (manual) OR the vacancy source channel
    if chat_id not in (YOUR_CHAT_ID, VACANCY_SOURCE_CHAT_ID):
        return

    text = msg.text or msg.caption or ""
    if not text:
        return

    # If from vacancy channel — all status updates go to YOUR personal chat
    await _notify(ctx, "⏳ Новая вакансия, парсю...")

    try:
        jd = await extract_jd(text)
    except Exception as e:
        await _notify(ctx, f"❌ Не смог достать JD: {e}")
        return

    try:
        score_data = score_vacancy(jd)
    except Exception as e:
        await _notify(ctx, f"❌ Ошибка скоринга: {e}")
        return

    score     = score_data.get("score", 0)
    reason    = score_data.get("reason", "нет причины")
    role_title = score_data.get("role_title", "Python Developer")
    company   = score_data.get("company", "Компания")

    # Always tell why — no silent skips
    if score < SCORE_THRESHOLD:
        await _notify(
            ctx,
            f"⏭ *{company} — {role_title}*\nСкип ({score}/10)\n\n_{reason}_",
            parse_mode="Markdown"
        )
        return

    await _notify(
        ctx,
        f"✅ *{company} — {role_title}* ({score}/10)\n_{reason}_\n\nГенерирую...",
        parse_mode="Markdown"
    )

    try:
        result = generate_application(jd, _base_cv())
    except Exception as e:
        await _notify(ctx, f"❌ Ошибка генерации: {e}")
        return

    cv_text    = result.get("cv_text", _base_cv())
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
        caption=f"📄 CV для {company}"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Отправить", callback_data=f"send:{pdf_msg.message_id}"),
        InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit:{pdf_msg.message_id}"),
        InlineKeyboardButton("❌ Пропустить",    callback_data=f"skip:{pdf_msg.message_id}"),
    ]])

    await ctx.bot.send_message(
        chat_id=YOUR_CHAT_ID,
        text=f"💬 Сообщение для рекрутера:\n\n{tg_message}",
        reply_markup=keyboard
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
            await query.edit_message_text("⚠️ Не нашла данные, попробуй заново.")
            return
        # Just confirm — actual sending to recruiter is manual (you copy-paste)
        # TODO: if you want auto-send, add recruiter contact to payload
        await query.edit_message_text(
            f"✅ Готово к отправке!\n\n{payload['tg_message']}\n\n"
            f"_(скопируй и отправь рекрутеру вместе с PDF)_",
            parse_mode="Markdown"
        )
        delete_pending(ref_id)

    elif action == "edit":
        await query.edit_message_text(
            f"✏️ Отредактируй и отправь мне обратно:\n\n{payload['tg_message']}"
        )
        # Next message from user will be treated as new vacancy — 
        # for now editing is manual. Can add ConversationHandler for proper edit flow.
