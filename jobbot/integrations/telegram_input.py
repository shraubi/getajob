"""Extract job URLs from Telegram messages, including hidden entities/buttons."""


def telegram_message_url(msg) -> str:
    text = msg.text or msg.caption or ""
    for entity in tuple(msg.entities or ()) + tuple(msg.caption_entities or ()):
        if entity.type == "text_link" and entity.url:
            return entity.url
        if entity.type == "url":
            return text[entity.offset:entity.offset + entity.length]
    if msg.reply_markup:
        for row in msg.reply_markup.inline_keyboard:
            for button in row:
                if button.url:
                    return button.url
    return ""
