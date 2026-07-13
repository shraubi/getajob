"""Discover fresh Hirify jobs and run the deterministic rating chain."""

from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .rating import rate_job


def extract_job_urls(html: str, feed_url: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(feed_url, str(anchor["href"]))
        parsed = urlparse(url)
        if parsed.hostname not in {"hirify.me", "www.hirify.me"} or not parsed.path.startswith("/jobs/"):
            continue
        canonical = f"https://hirify.me{parsed.path}"
        if canonical not in seen:
            seen.add(canonical)
            urls.append(canonical)
    return urls


async def discover(feed_url: str, limit: int) -> list[dict[str, object]]:
    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "getajob-ralph/0.1 (+deterministic compatibility tester)"},
    ) as client:
        response = await client.get(feed_url)
        response.raise_for_status()
    reports: list[dict[str, object]] = []
    for url in extract_job_urls(response.text, feed_url)[:limit]:
        try:
            report = await rate_job(url)
            reports.append(report.to_dict())
        except Exception as exc:
            reports.append({"url": url, "status": "failed", "discovery_error": f"{type(exc).__name__}: {exc}"})
    return reports


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed-url", default="https://hirify.me/")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(discover(args.feed_url, args.limit)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

