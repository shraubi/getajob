"""Ashby public-job parsing, semantic preflight, and browser-assisted submission."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from jobbot.application import Vacancy
from jobbot.integrations.job_page import ParsedJobPage, validate_public_url
from jobbot.integrations.web_application import load_profile

_GRAPHQL_URL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
_ASHBY_HOST = "jobs.ashbyhq.com"
_PATH_RE = re.compile(
    r"^/(?P<board>[^/?#]+)/(?P<job>[0-9a-fA-F-]{36})(?:/application)?/?$"
)
_JOB_QUERY = """
query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
    jobPostingId: $jobPostingId
  ) {
    id
    title
    departmentName
    locationName
    workplaceType
    employmentType
    descriptionHtml
    linkedData
    applicationForm {
      id
      sections {
        title
        fieldEntries {
          id
          field
          isRequired
          descriptionHtml
        }
      }
    }
  }
}
"""


class AshbyError(RuntimeError):
    pass


@dataclass(frozen=True)
class AshbyField:
    path: str
    title: str
    field_type: str
    required: bool
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class AshbyPosting:
    page: ParsedJobPage
    board: str
    job_posting_id: str
    fields: tuple[AshbyField, ...]


@dataclass(frozen=True)
class AshbyPreflight:
    posting: AshbyPosting
    submissions: dict[str, object]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class AshbySubmissionResult:
    status: str
    url: str
    detail: str = ""


BrowserSubmitter = Callable[
    [AshbyPreflight, Path, Path, bool],
    Awaitable[AshbySubmissionResult],
]


def is_ashby_job_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname == _ASHBY_HOST
        and bool(_PATH_RE.match(parsed.path))
    )


def parse_ashby_url(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    match = _PATH_RE.match(parsed.path)
    if parsed.hostname != _ASHBY_HOST or not match:
        raise AshbyError("Unsupported Ashby URL; expected https://jobs.ashbyhq.com/<board>/<job-id>")
    board = match.group("board")
    job_id = match.group("job").lower()
    canonical = f"https://{_ASHBY_HOST}/{board}/{job_id}"
    return board, job_id, canonical


def _field_options(field: dict) -> tuple[str, ...]:
    metadata = field.get("metadata") if isinstance(field.get("metadata"), dict) else {}
    candidates = (
        field.get("selectableValues")
        or metadata.get("selectableValues")
        or metadata.get("options")
        or ()
    )
    values: list[str] = []
    for item in candidates:
        if isinstance(item, dict):
            value = item.get("value") or item.get("label") or item.get("title")
        else:
            value = item
        if value is not None:
            values.append(str(value))
    return tuple(values)


def _parse_fields(form: dict) -> tuple[AshbyField, ...]:
    result: list[AshbyField] = []
    for section in form.get("sections") or ():
        for entry in section.get("fieldEntries") or ():
            field = entry.get("field") or {}
            path = str(field.get("path") or field.get("id") or entry.get("id") or "").strip()
            if not path:
                continue
            result.append(
                AshbyField(
                    path=path,
                    title=str(field.get("title") or field.get("humanReadablePath") or path).strip(),
                    field_type=str(field.get("type") or "String"),
                    required=bool(entry.get("isRequired")),
                    options=_field_options(field),
                )
            )
    return tuple(result)


async def fetch_ashby_posting(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AshbyPosting:
    board, job_id, canonical = parse_ashby_url(url)
    await validate_public_url(canonical)
    request = {
        "operationName": "ApiJobPosting",
        "variables": {
            "organizationHostedJobsPageName": board,
            "jobPostingId": job_id,
        },
        "query": _JOB_QUERY,
    }
    async with httpx.AsyncClient(timeout=20.0, transport=transport) as client:
        response = await client.post(
            _GRAPHQL_URL,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json=request,
        )
        response.raise_for_status()
        payload = response.json()
    if payload.get("errors"):
        raise AshbyError("Ashby returned an error for this vacancy")
    posting = (payload.get("data") or {}).get("jobPosting")
    if not posting:
        raise AshbyError("Ashby vacancy was not found or is no longer public")

    description = BeautifulSoup(
        str(posting.get("descriptionHtml") or ""), "html.parser"
    ).get_text("\n", strip=True)
    linked = posting.get("linkedData") if isinstance(posting.get("linkedData"), dict) else {}
    organization = (
        linked.get("hiringOrganization")
        if isinstance(linked.get("hiringOrganization"), dict)
        else {}
    )
    company = str(organization.get("name") or board).strip()
    title = str(posting.get("title") or "").strip()
    if not title or len(description) < 40:
        raise AshbyError("Ashby did not expose enough vacancy information")

    vacancy = Vacancy(
        title=title[:160],
        company=company[:120],
        description=description[:20_000],
        url=canonical,
        source_category="ats",
        location=str(posting.get("locationName") or ""),
        work_format=str(posting.get("workplaceType") or ""),
        employment=str(posting.get("employmentType") or ""),
    )
    page = ParsedJobPage(
        vacancy=vacancy,
        source_category="ashby_application_form",
        apply_url=canonical + "/application",
        fetched_url=canonical,
        contact_kind="ashby",
        contact_value=job_id,
    )
    fields = _parse_fields(posting.get("applicationForm") or {})
    if not fields:
        raise AshbyError("Ashby vacancy has no public application form contract")
    return AshbyPosting(page=page, board=board, job_posting_id=job_id, fields=fields)


def _answer_lookup(raw: dict, field: AshbyField):
    answers = raw.get("answers") if isinstance(raw.get("answers"), dict) else {}
    for key in (field.path, field.title):
        if key in answers:
            return answers[key]
    folded = field.title.casefold()
    for key, value in answers.items():
        if str(key).casefold() == folded:
            return value
    return None


def _standard_value(field: AshbyField, raw: dict, profile):
    path = field.path.casefold()
    title = field.title.casefold()
    full_name = " ".join(part for part in (profile.first_name, profile.last_name) if part).strip()
    if path == "_systemfield_name" or title == "name":
        return full_name
    if path == "_systemfield_email" or field.field_type == "Email":
        return profile.email
    if path == "_systemfield_phone" or field.field_type == "Phone":
        return profile.phone
    if path == "_systemfield_resume" or (field.field_type == "File" and "resume" in title):
        return "__resume__"
    if path == "_systemfield_location" or field.field_type == "Location":
        return raw.get("location")
    links = raw.get("links") if isinstance(raw.get("links"), dict) else {}
    if field.field_type == "SocialLink" or any(word in title for word in ("linkedin", "github", "portfolio", "website")):
        for key in ("linkedin", "github", "portfolio", "website"):
            if key in title and links.get(key):
                return links[key]
    return None


def _coerce_value(field: AshbyField, value):
    if value is None or value == "":
        return None
    kind = field.field_type
    if kind == "Boolean":
        if isinstance(value, bool):
            return value
        folded = str(value).casefold()
        if folded in {"true", "yes", "1", "on"}:
            return True
        if folded in {"false", "no", "0", "off"}:
            return False
        return value
    if kind == "Number":
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if kind == "MultiValueSelect" and not isinstance(value, list):
        return [str(value)]
    return value


async def preflight_ashby_application(
    url: str,
    resume_path: Path,
    profile_path: Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AshbyPreflight:
    if not resume_path.is_file():
        raise AshbyError(f"Resume is missing: {resume_path.name}")
    if resume_path.stat().st_size > 50_000_000:
        raise AshbyError("Resume exceeds Ashby's 50 MB upload limit")
    posting = await fetch_ashby_posting(url, transport=transport)
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.is_file() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise AshbyError(f"Applicant profile is invalid: {exc}") from exc
    profile = load_profile(profile_path, resume_path)
    submissions: dict[str, object] = {}
    missing: list[str] = []
    for field in posting.fields:
        value = _answer_lookup(raw, field)
        if value is None:
            value = _standard_value(field, raw, profile)
        value = _coerce_value(field, value)
        if value is None or value == "" or value == []:
            if field.required:
                missing.append(f"{field.title} [{field.path}]")
            continue
        if field.options:
            supplied = value if isinstance(value, list) else [value]
            if any(str(item) not in field.options for item in supplied):
                if field.required:
                    missing.append(
                        f"{field.title} [{field.path}] — choose one of: {', '.join(field.options)}"
                    )
                continue
        submissions[field.path] = value
    return AshbyPreflight(posting, submissions, tuple(missing))


def format_missing_questions(preflight: AshbyPreflight) -> str:
    if not preflight.missing:
        return ""
    return "Required Ashby answers are missing:\n" + "\n".join(
        f"• {item}" for item in preflight.missing
    )


async def _submit_with_playwright(
    preflight: AshbyPreflight,
    resume_path: Path,
    browser_profile_path: Path,
    headless: bool,
) -> AshbySubmissionResult:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise AshbyError("Playwright is not installed for Ashby browser submission") from exc

    browser_profile_path.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(browser_profile_path),
            headless=headless,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(preflight.posting.page.apply_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(1_000)

            for field in preflight.posting.fields:
                if field.path not in preflight.submissions:
                    continue
                value = preflight.submissions[field.path]
                if value == "__resume__":
                    control = page.locator("#_systemfield_resume")
                    if await control.count() != 1:
                        control = page.get_by_label(field.title, exact=True)
                    if await control.count() != 1:
                        raise AshbyError(f"Could not locate Ashby field: {field.title}")
                    await control.set_input_files(str(resume_path))
                    continue

                control = page.get_by_label(field.title, exact=True)
                count = await control.count()
                if count == 1:
                    if field.field_type == "Boolean":
                        await control.select_option("true" if value else "false")
                    elif field.field_type == "MultiValueSelect":
                        await control.select_option([str(item) for item in value])
                    elif field.field_type == "ValueSelect":
                        await control.select_option(str(value))
                    else:
                        await control.fill(
                            json.dumps(value) if isinstance(value, dict) else str(value)
                        )
                    continue

                title = page.get_by_text(field.title, exact=True)
                if await title.count() != 1:
                    raise AshbyError(f"Could not locate Ashby field: {field.title}")
                container = title.locator("xpath=..")
                option = container.get_by_role(
                    "button", name=("Yes" if value is True else "No" if value is False else str(value)),
                    exact=True,
                )
                if await option.count() != 1:
                    option = container.get_by_label(str(value), exact=True)
                if await option.count() != 1:
                    raise AshbyError(f"Could not select Ashby answer for: {field.title}")
                await option.click()

            submit = page.get_by_role("button", name="Submit Application", exact=True)
            if await submit.count() != 1:
                raise AshbyError("Ashby submit button was not found")
            await submit.click()

            for _ in range(40):
                await page.wait_for_timeout(500)
                body = (await page.locator("body").inner_text()).casefold()
                if any(marker in body for marker in (
                    "application submitted", "thank you for applying", "application received"
                )):
                    return AshbySubmissionResult("submitted", page.url, "Ashby confirmation page")
                challenge = page.locator('iframe[title*="challenge" i], iframe[src*="/bframe"]')
                if await challenge.count() > 0 and await challenge.first.is_visible():
                    return AshbySubmissionResult(
                        "manual_required",
                        preflight.posting.page.apply_url,
                        "Ashby requested an interactive reCAPTCHA challenge",
                    )
                alerts = page.locator('[role="alert"]')
                if await alerts.count() > 0:
                    messages = [text.strip() for text in await alerts.all_inner_texts() if text.strip()]
                    if messages:
                        return AshbySubmissionResult("failed", page.url, "; ".join(messages)[:1000])
            return AshbySubmissionResult(
                "manual_required",
                preflight.posting.page.apply_url,
                "Ashby did not return a verifiable success confirmation",
            )
        finally:
            await context.close()


async def submit_ashby_application(
    url: str,
    resume_path: Path,
    profile_path: Path,
    browser_profile_path: Path,
    *,
    headless: bool = True,
    browser_submitter: BrowserSubmitter | None = None,
) -> AshbySubmissionResult:
    preflight = await preflight_ashby_application(url, resume_path, profile_path)
    if preflight.missing:
        raise AshbyError(format_missing_questions(preflight))
    submitter = browser_submitter or _submit_with_playwright
    return await submitter(preflight, resume_path, browser_profile_path, headless)
