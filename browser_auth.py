"""Browser-backed authentication shared by protected job-source adapters."""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


class BrowserAuthError(RuntimeError):
    pass


async def login_hirify(
    email: str,
    password: str,
    state_path: Path,
    executable_path: str = "/usr/bin/chromium",
) -> None:
    """Use the real Hirify popup and persist the resulting browser session."""
    if not email or not password:
        raise BrowserAuthError("Hirify credentials are not configured")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserAuthError("Browser authentication support is not installed") from exc

    state_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=executable_path,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto("https://hirify.me/", wait_until="domcontentloaded", timeout=30_000)
            sign_in = page.get_by_role("button", name=re.compile(r"^(Sign In|Ð’Ð¾Ð¹Ñ‚Ð¸)$", re.I))
            await sign_in.click(timeout=10_000)
            form = page.locator("form").filter(has=page.locator("#email"))
            await form.locator("#email").fill(email)
            await form.locator("#password").fill(password)
            async with page.expect_response(lambda response: "/auth/login" in response.url, timeout=20_000) as pending:
                await form.locator('button[type="submit"]').click()
            response = await pending.value
            if response.status >= 400:
                raise BrowserAuthError(f"Hirify popup login failed (HTTP {response.status})")
            await context.storage_state(path=str(state_path))
            logger.info("Hirify browser session refreshed login_url=%s status=%s", response.url, response.status)
        except BrowserAuthError:
            raise
        except Exception as exc:
            raise BrowserAuthError("Hirify popup login failed") from exc
        finally:
            await browser.close()
