"""One-time visible HelloWork login that exports Playwright storage state."""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobbot import config


async def main() -> None:
    from playwright.async_api import async_playwright

    target = config.HELLOWORK_AUTH_STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://www.hellowork.com/fr-fr/candidat/connexion.html")
        await asyncio.to_thread(
            input,
            "Sign in to HelloWork in the browser, complete any verification, then press Enter here: ",
        )
        await context.storage_state(path=str(target))
        await browser.close()
    print(f"HelloWork authentication saved to {target}")


if __name__ == "__main__":
    asyncio.run(main())
