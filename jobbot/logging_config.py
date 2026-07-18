"""Production logging policy shared by application entry points."""

import logging


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO)
    # httpx logs full request URLs at INFO. Telegram embeds the bot token in
    # that URL, so dependency-level request logging must never be enabled in
    # production output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
