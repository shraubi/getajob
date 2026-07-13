"""Create or update a GitHub issue from a deterministic Ralph payload."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx

from .store import mark_issue_created


class GitHubIssueError(RuntimeError):
    pass


async def sync_issue(
    payload: dict[str, object],
    *,
    repository: str,
    token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    if not repository or "/" not in repository:
        raise GitHubIssueError("GITHUB_REPOSITORY must use owner/name format")
    if not token:
        raise GitHubIssueError("GITHUB_TOKEN is required")
    fingerprint = str(payload["fingerprint"])
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        headers=headers,
        timeout=20.0,
        transport=transport,
    ) as client:
        response = await client.get(f"/repos/{repository}/issues", params={"state": "open", "per_page": 100})
        response.raise_for_status()
        existing = next(
            (
                issue for issue in response.json()
                if "pull_request" not in issue and fingerprint in str(issue.get("body", ""))
            ),
            None,
        )
        data = {"title": payload["title"], "body": payload["body"]}
        if existing:
            response = await client.patch(f"/repos/{repository}/issues/{existing['number']}", json=data)
        else:
            response = await client.post(f"/repos/{repository}/issues", json=data)
        response.raise_for_status()
        issue = response.json()
    return {"number": int(issue["number"]), "html_url": str(issue["html_url"]), "created": not bool(existing)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, default=Path("storage/ralph_issue.json"))
    parser.add_argument("--db", type=Path, default=Path("storage/ralph.db"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args()
    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    import asyncio

    result = asyncio.run(sync_issue(
        payload,
        repository=args.repository,
        token=os.environ.get("GITHUB_TOKEN", ""),
    ))
    mark_issue_created(args.db, str(payload["fingerprint"]), int(result["number"]))
    print(json.dumps(result))


if __name__ == "__main__":
    main()

