"""Production logging policy shared by application entry points."""

import logging


def configure_logging() -> None:
    # Imported dependencies may configure the root logger before the production
    # entry point runs. Replace that configuration so startup and fatal errors
    # always reach Docker's stdout/stderr stream.
    logging.basicConfig(level=logging.INFO, force=True)
    # httpx logs full request URLs at INFO. Telegram embeds the bot token in
    # that URL, so dependency-level request logging must never be enabled in
    # production output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
