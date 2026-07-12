"""One-time Hirify browser login that persists Playwright storage state."""

import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright


async def main() -> None:
    email = os.environ["HIRIFY_EMAIL"]
    password = os.environ["HIRIFY_PASSWORD"]
    state_path = Path(os.environ.get("HIRIFY_STATE_PATH", "/app/storage/hirify_state.json"))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://hirify.me/", wait_until="domcontentloaded")
        await page.get_by_role("button", name="Sign In", exact=True).click()
        await page.get_by_label("Email", exact=True).fill(email)
        await page.get_by_label("Password", exact=True).fill(password)
        await page.get_by_role("button", name="Login", exact=True).click()
        await page.get_by_role("button", name="Sign In", exact=True).wait_for(state="hidden", timeout=20_000)
        await context.storage_state(path=state_path)
        await browser.close()
    print(f"Hirify browser session saved: {state_path}")


if __name__ == "__main__":
    asyncio.run(main())
