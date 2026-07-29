"""Create a transferable HelloWork browser session using a local visible browser.

This file is intentionally standalone: it does not import jobbot configuration and
can be downloaded and run without cloning the repository.
"""

import argparse
import asyncio
from pathlib import Path


async def create_session(target: Path) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is required. Run: py -m pip install playwright && "
            "py -m playwright install chromium"
        ) from exc

    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://www.hellowork.com/fr-fr/candidat/connexion.html")
        await asyncio.to_thread(
            input,
            "Sign in to HelloWork in the browser, complete any verification, "
            "then press Enter here: ",
        )
        await context.storage_state(path=str(target))
        await browser.close()

    print(f"HelloWork authentication saved to {target}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("hellowork-auth.json"),
        help="Session file to create (default: ./hellowork-auth.json)",
    )
    args = parser.parse_args()
    asyncio.run(create_session(args.output))


if __name__ == "__main__":
    main()
