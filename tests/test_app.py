import logging
import unittest

from jobbot.logging_config import configure_logging


class AppLoggingTests(unittest.TestCase):
    def test_dependency_request_logs_cannot_expose_telegram_token_urls(self):
        httpx_logger = logging.getLogger("httpx")
        httpcore_logger = logging.getLogger("httpcore")
        previous_httpx = httpx_logger.level
        previous_httpcore = httpcore_logger.level
        try:
            httpx_logger.setLevel(logging.INFO)
            httpcore_logger.setLevel(logging.INFO)
            configure_logging()
            self.assertGreaterEqual(httpx_logger.level, logging.WARNING)
            self.assertGreaterEqual(httpcore_logger.level, logging.WARNING)
        finally:
            httpx_logger.setLevel(previous_httpx)
            httpcore_logger.setLevel(previous_httpcore)


if __name__ == "__main__":
    unittest.main()
