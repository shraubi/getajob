"""Launch Chromium once with the same settings used by HelloWork submissions."""

from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from jobbot import config


def _reason(exc: Exception) -> str:
    detail = str(exc).casefold()
    if "executable doesn't exist" in detail:
        return "browser_missing"
    if "host system is missing dependencies" in detail or "error while loading shared libraries" in detail:
        return "browser_dependencies_missing"
    if "permission denied" in detail or "eacces" in detail:
        return "browser_permission_denied"
    if "no space left on device" in detail:
        return "disk_full"
    if "no display server" in detail or "missing x server" in detail:
        return "display_required"
    if "sandbox" in detail:
        return "browser_sandbox"
    if "sigkill" in detail or "out of memory" in detail:
        return "browser_out_of_memory"
    return "browser_launch_unknown"


async def probe() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=config.ATS_BROWSER_HEADLESS)
        await browser.close()


def main() -> int:
    try:
        asyncio.run(probe())
    except Exception as exc:
        print(f"browser_launch=FAIL reason={_reason(exc)}")
        return 1
    print("browser_launch=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
