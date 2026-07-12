import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("YOUR_CHAT_ID", "1")

from telegram_input import telegram_message_url


class HandlerUrlTests(unittest.TestCase):
    def test_extracts_hidden_text_link_from_forwarded_lead(self):
        url = "https://hirify.me/jobs/711565-backend-engineer-typescript-nestjs"
        msg = SimpleNamespace(
            text="Backend Engineer (TypeScript) Ð² Supabase\nÐ£Ð´Ð°Ð»ÐµÐ½Ð½Ð¾ | senior",
            caption=None,
            entities=(SimpleNamespace(type="text_link", url=url, offset=0, length=34),),
            caption_entities=(),
            reply_markup=None,
        )
        self.assertEqual(telegram_message_url(msg), url)

    def test_extracts_url_from_forwarded_inline_button(self):
        url = "https://hirify.me/jobs/711565-backend-engineer-typescript-nestjs"
        button = SimpleNamespace(url=url)
        msg = SimpleNamespace(
            text="Backend Engineer", caption=None, entities=(), caption_entities=(),
            reply_markup=SimpleNamespace(inline_keyboard=((button,),)),
        )
        self.assertEqual(telegram_message_url(msg), url)


if __name__ == "__main__":
    unittest.main()
