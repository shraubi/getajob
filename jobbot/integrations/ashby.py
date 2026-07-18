"""Ashby public-job parsing, semantic preflight, and browser-assisted submission."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from jobbot.application import Vacancy
from jobbot.integrations.form_matching import (
    best_field_label_match,
    best_submit_control_match,
    classify_form_submission,
    normalize_field_label,
)
from jobbot.integrations.job_page import ParsedJobPage, validate_public_url
from jobbot.integrations.web_application import load_profile

_GRAPHQL_URL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
_ASHBY_HOST = "jobs.ashbyhq.com"
_PATH_RE = re.compile(
    r"^/(?P<board>[^/?#]+)/(?P<job>[0-9a-fA-F-]{36})(?:/application)?/?$"
)
_DIAGNOSTIC_EMAIL_RE = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)
_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?i)\b(authorization|cookie|password|secret|token)\s*[:=]\s*\S+"
)
_RECAPTCHA_IFRAME_SELECTOR = (
    'iframe[title*="recaptcha" i], iframe[title*="challenge" i], '
    'iframe[src*="/anchor"], iframe[src*="/bframe"]'
)
logger = logging.getLogger(__name__)


def _diagnostic_text(value: object, *, limit: int = 300) -> str:
    """Return bounded diagnostic text without applicant data or credentials."""
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = _DIAGNOSTIC_EMAIL_RE.sub("[email]", text)
    text = _DIAGNOSTIC_SECRET_RE.sub(r"\1=[redacted]", text)
    if len(text) > limit:
        return text[: limit - 1] + "â€¦"
    return text


def _diagnostic_url(value: object) -> str:
    parsed = urlparse(str(value))
    if not parsed.scheme or not parsed.netloc:
        return _diagnostic_text(value)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _append_diagnostic(events: list[str], value: object) -> None:
    event = _diagnostic_text(value)
    if event:
        events.append(event)
        del events[:-20]


def _recaptcha_requires_user(
    *,
    control_present: bool,
    challenge_visible: bool,
    token_present: bool,
) -> bool:
    """Return whether Ashby's reCAPTCHA gate still needs human input."""
    return (
        (control_present or challenge_visible)
        and not token_present
    )


_FIELD_CONTAINER_XPATH = (
    "xpath=ancestor::*["
    "self::fieldset or "
    "contains(concat(' ', normalize-space(@class), ' '), "
    "' ashby-application-form-field-entry ')"
    "][1]"
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


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _facts(raw: dict) -> dict:
    return raw.get("facts") if isinstance(raw.get("facts"), dict) else {}


def _country(raw: dict) -> str:
    location = raw.get("location")
    if isinstance(location, dict):
        return str(location.get("country") or "").strip()
    return str(_facts(raw).get("country") or "").strip()


def _matching_option(field: AshbyField, preferences) -> str | None:
    if not field.options:
        return None
    if isinstance(preferences, str):
        preferences = [preferences]
    if not isinstance(preferences, list):
        return None
    by_name = {_normalized(option): option for option in field.options}
    for preference in preferences:
        match = by_name.get(_normalized(preference))
        if match:
            return match
    return None


def _semantic_value(field: AshbyField, raw: dict, posting: AshbyPosting):
    facts = _facts(raw)
    title = _normalized(field.title)
    country = _country(raw)

    authorization_question = any(phrase in title for phrase in (
        "authorized to work",
        "authorised to work",
        "work authorization",
        "work authorisation",
        "legally eligible to work",
        "right to work",
    ))
    if field.field_type == "Boolean" and authorization_question:
        authorized = facts.get("work_authorized_countries")
        if not isinstance(authorized, list) or not country:
            return None
        authorized_names = {_normalized(item) for item in authorized}
        any_country = any(str(item).strip() == "*" or _normalized(item) == "any" for item in authorized)
        return any_country or _normalized(country) in authorized_names

    previous_employer_question = any(phrase in title for phrase in (
        "worked for",
        "worked at",
        "previously employed by",
        "ever been employed by",
    ))
    if field.field_type == "Boolean" and previous_employer_question:
        if "previous_employers" not in facts or not isinstance(facts["previous_employers"], list):
            return None
        company = _normalized(posting.page.vacancy.company)
        employers = {_normalized(item) for item in facts["previous_employers"]}
        return company in employers

    source_question = any(phrase in title for phrase in (
        "how did you hear",
        "how did you find",
        "how did you learn",
        "where did you hear",
        "source of application",
    ))
    if source_question:
        return _matching_option(field, facts.get("application_source_preferences"))

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
        if value is None:
            value = _semantic_value(field, raw, posting)
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
                        f"{field.title} [{field.path}] â€” choose one of: {', '.join(field.options)}"
                    )
                continue
        submissions[field.path] = value
    return AshbyPreflight(posting, submissions, tuple(missing))


def format_missing_questions(preflight: AshbyPreflight) -> str:
    if not preflight.missing:
        return ""
    return "Required Ashby answers are missing:\n" + "\n".join(
        f"â€¢ {item}" for item in preflight.missing
    )


async def _resolve_submit_control(page):
    """Resolve a matched action by semantic identity, never a live DOM index."""
    candidates = page.locator(
        'button:visible, input[type="submit"]:visible, [role="button"]:visible'
    )
    labels = await candidates.evaluate_all(
        """(elements) => elements.map((element) => (
            element.innerText ||
            element.value ||
            element.getAttribute("aria-label") ||
            element.getAttribute("title") ||
            ""
        ).trim())"""
    )
    match_index = best_submit_control_match(labels)
    if match_index is None:
        raise AshbyError(
            "Could not identify a unique submit control "
            f"(visible controls={labels})"
        )

    accessible_name = str(labels[match_index]).strip()
    control = page.get_by_role(
        "button",
        name=accessible_name,
        exact=True,
    )
    count = await control.count()
    if count != 1:
        raise AshbyError(
            "Matched submit intent but could not re-resolve one stable "
            f"semantic control (name={accessible_name!r}, count={count}, "
            f"visible controls={labels})"
        )
    return control, accessible_name, labels


async def _submit_with_playwright(
    preflight: AshbyPreflight,
    resume_path: Path,
    browser_profile_path: Path,
    headless: bool,
) -> AshbySubmissionResult:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
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
            diagnostic_events: list[str] = []

            def capture_console(message) -> None:
                if message.type in {"warning", "error"}:
                    _append_diagnostic(
                        diagnostic_events,
                        f"console {message.type}: {message.text}",
                    )

            def capture_page_error(error) -> None:
                _append_diagnostic(
                    diagnostic_events,
                    f"page error: {type(error).__name__}: {error}",
                )

            def capture_request_failure(request) -> None:
                _append_diagnostic(
                    diagnostic_events,
                    f"request failed: {request.method} "
                    f"{_diagnostic_url(request.url)} ({request.failure})",
                )

            def capture_response(response) -> None:
                if response.request.method == "POST" or response.status >= 400:
                    _append_diagnostic(
                        diagnostic_events,
                        f"response: {response.request.method} {response.status} "
                        f"{_diagnostic_url(response.url)}",
                    )

            page.on("console", capture_console)
            page.on("pageerror", capture_page_error)
            page.on("requestfailed", capture_request_failure)
            page.on("response", capture_response)

            logger.info(
                "Ashby submission opening url=%s headless=%s fields=%d",
                _diagnostic_url(preflight.posting.page.apply_url),
                headless,
                len(preflight.submissions),
            )
            await page.goto(preflight.posting.page.apply_url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.locator(".ashby-application-form-field-entry").first.wait_for(
                    state="visible",
                    timeout=20_000,
                )
            except PlaywrightTimeoutError as exc:
                raise AshbyError(
                    "Ashby application form did not render within 20 seconds "
                    f"(url={page.url}, title={await page.title()!r})"
                ) from exc

            for field in preflight.posting.fields:
                if field.path not in preflight.submissions:
                    continue
                value = preflight.submissions[field.path]

                # Resolve against all visible labels with provider-neutral
                # normalization. This handles whitespace, non-breaking spaces,
                # punctuation, accents, and harmless presentation wording.
                titles = page.locator(
                    ".ashby-application-form-question-title"
                )
                visible_titles = await titles.all_inner_texts()
                match_index = best_field_label_match(
                    field.title, visible_titles
                )
                container = page.locator(
                    '[data-jobbot-field-match="missing"]'
                )
                if match_index is not None:
                    container = titles.nth(match_index).locator(
                        _FIELD_CONTAINER_XPATH
                    )

                if await container.count() == 0:
                    exact_name = f'[name={json.dumps(field.path)}]'
                    suffix_name = f'[name$={json.dumps(field.path)}]'
                    exact_id = f'[id={json.dumps(field.path)}]'
                    suffix_id = f'[id$={json.dumps(field.path)}]'
                    control = page.locator(
                        f"{exact_name}, {suffix_name}, {exact_id}, {suffix_id}"
                    )
                    if await control.count() > 0:
                        container = control.first.locator(
                            _FIELD_CONTAINER_XPATH
                        )

                if await container.count() == 0:
                    semantic_selector = {
                        "Email": 'input[type="email"]',
                        "Phone": 'input[type="tel"]',
                        "Number": 'input[type="number"]',
                    }.get(field.field_type)
                    if value == "__resume__":
                        semantic_selector = 'input[type="file"]'
                    if semantic_selector:
                        semantic_control = page.locator(semantic_selector)
                        if await semantic_control.count() == 1:
                            container = semantic_control.locator(
                                _FIELD_CONTAINER_XPATH
                            )

                if await container.count() == 0:
                    raise AshbyError(
                        f"Could not locate form field: {field.title} "
                        f"(normalized={normalize_field_label(field.title)!r}, "
                        f"visible questions={visible_titles})"
                    )
                container = container.first

                if value == "__resume__":
                    upload = container.locator('input[type="file"]')
                    if await upload.count() == 0:
                        raise AshbyError(f"Could not locate upload control for: {field.title}")
                    await upload.first.set_input_files(str(resume_path))
                    continue

                answer = (
                    "Yes" if value is True
                    else "No" if value is False
                    else str(value)
                )
                native_select = container.locator("select:visible")
                if await native_select.count() > 0:
                    if field.field_type == "MultiValueSelect":
                        await native_select.first.select_option([str(item) for item in value])
                    else:
                        await native_select.first.select_option(
                            "true" if value is True else "false" if value is False else str(value)
                        )
                    continue

                if field.field_type in {"Boolean", "ValueSelect", "MultiValueSelect"}:
                    answers = value if isinstance(value, list) else [value]
                    for item in answers:
                        item_text = (
                            "Yes" if item is True
                            else "No" if item is False
                            else str(item)
                        )
                        option = container.get_by_label(item_text, exact=True)
                        if await option.count() == 0:
                            option = container.get_by_role("button", name=item_text, exact=True)
                        if await option.count() == 0:
                            option = container.get_by_text(item_text, exact=True)
                        if await option.count() == 0:
                            raise AshbyError(
                                f"Could not select Ashby answer {item_text!r} for: {field.title}"
                            )
                        await option.first.click()
                    continue

                control = container.locator(
                    'input:not([type="radio"]):not([type="checkbox"]):visible, '
                    "textarea:visible, [role=\"combobox\"]:visible"
                )
                if await control.count() == 0:
                    raise AshbyError(f"Could not locate input control for: {field.title}")
                control = control.first
                if isinstance(value, dict):
                    if "country" in _normalized(field.title):
                        text_value = str(value.get("country") or "").strip()
                    else:
                        text_value = ", ".join(
                            str(value.get(key)).strip()
                            for key in ("city", "region", "country")
                            if value.get(key)
                        )
                else:
                    text_value = str(value)
                await control.fill(text_value)

                if field.field_type == "Location":
                    option = page.get_by_role("option", name=text_value, exact=True)
                    try:
                        await option.first.wait_for(state="visible", timeout=5_000)
                    except PlaywrightTimeoutError as exc:
                        raise AshbyError(
                            f"Ashby location option did not appear for: {text_value}"
                        ) from exc
                    await option.first.click()

            submit_control, submit_name, submit_labels = (
                await _resolve_submit_control(page)
            )
            selected_submit_label = _diagnostic_text(submit_name)
            initial_url = page.url
            logger.info(
                "Ashby submission clicking control=%r enabled=%s url=%s "
                "visible_controls=%s",
                selected_submit_label,
                await submit_control.is_enabled(),
                _diagnostic_url(initial_url),
                [_diagnostic_text(label, limit=80) for label in submit_labels],
            )
            await submit_control.click()
            logger.info(
                "Ashby submission click completed control=%r url=%s",
                selected_submit_label,
                _diagnostic_url(page.url),
            )

            last_snapshot = ""
            # A visible browser can remain open while the applicant solves an
            # interactive reCAPTCHA. Headless runs fail fast with an actionable
            # handoff instead of waiting on a token that cannot be supplied.
            wait_seconds = 300 if not headless else 30
            for attempt in range(wait_seconds * 2):
                await page.wait_for_timeout(500)

                success_regions = page.locator(
                    ".ashby-application-form-success-container"
                )
                success_visible = False
                success_messages: list[str] = []
                for index in range(await success_regions.count()):
                    region = success_regions.nth(index)
                    if await region.is_visible():
                        success_visible = True
                        text = (await region.inner_text()).strip()
                        if text:
                            success_messages.append(text)

                failure_regions = page.locator(
                    '.ashby-application-form-failure-container, [role="alert"]'
                )
                failure_visible = False
                failure_messages: list[str] = []
                for index in range(await failure_regions.count()):
                    region = failure_regions.nth(index)
                    if await region.is_visible():
                        text = (await region.inner_text()).strip()
                        if text:
                            failure_visible = True
                            failure_messages.append(text)

                challenge = page.locator(_RECAPTCHA_IFRAME_SELECTOR)
                challenge_visible = False
                for index in range(await challenge.count()):
                    if await challenge.nth(index).is_visible():
                        challenge_visible = True
                        break

                recaptcha_response = page.locator(
                    'textarea[name="g-recaptcha-response"]'
                )
                recaptcha_control_present = (
                    await recaptcha_response.count() > 0
                )
                recaptcha_token_present = False
                if recaptcha_control_present:
                    recaptcha_token_present = await recaptcha_response.first.evaluate(
                        "(element) => Boolean(element.value)"
                    )
                challenge_requires_user = _recaptcha_requires_user(
                    control_present=recaptcha_control_present,
                    challenge_visible=challenge_visible,
                    token_present=recaptcha_token_present,
                )
                if challenge_requires_user and not headless and attempt == 0:
                    logger.warning(
                        "Ashby reCAPTCHA requires user interaction; waiting in "
                        "the visible browser for up to five minutes"
                    )

                outcome = classify_form_submission(
                    success_present=success_visible,
                    success_text="; ".join(success_messages)[:1000],
                    failure_present=failure_visible,
                    failure_text="; ".join(failure_messages)[:1000],
                    challenge_present=challenge_requires_user and headless,
                    challenge_text=(
                        "Ashby reCAPTCHA requires a visible browser and user "
                        "interaction"
                    ),
                )

                invalid_controls = page.locator(
                    'input:invalid:visible, textarea:invalid:visible, '
                    'select:invalid:visible, [aria-invalid="true"]:visible'
                )
                invalid_details = await invalid_controls.evaluate_all(
                    """(elements) => elements.map((element) => ({
                        field: (
                            element.getAttribute("aria-label") ||
                            element.name ||
                            element.id ||
                            element.type ||
                            element.tagName
                        ),
                        message: (
                            element.validationMessage ||
                            element.getAttribute("aria-errormessage") ||
                            ""
                        )
                    }))"""
                )
                live_texts = [
                    _diagnostic_text(text, limit=160)
                    for text in await page.locator(
                        '[role="status"]:visible, [aria-live]:visible'
                    ).all_inner_texts()
                    if text.strip()
                ][:6]
                heading_texts = [
                    _diagnostic_text(text, limit=120)
                    for text in await page.locator(
                        "h1:visible, h2:visible, h3:visible"
                    ).all_inner_texts()
                    if text.strip()
                ][:6]
                current_submit_labels = await page.locator(
                    'button:visible, input[type="submit"]:visible, '
                    '[role="button"]:visible'
                ).evaluate_all(
                    """(elements) => elements.map((element) => (
                        element.innerText ||
                        element.value ||
                        element.getAttribute("aria-label") ||
                        element.getAttribute("title") ||
                        ""
                    ).trim())"""
                )
                form_still_present = await page.locator(
                    ".ashby-application-form-field-entry, input[type=\"file\"]"
                ).count() > 0
                snapshot = _diagnostic_text(
                    {
                        "url": _diagnostic_url(page.url),
                        "form_present": form_still_present,
                        "submit_controls": [
                            _diagnostic_text(label, limit=80)
                            for label in current_submit_labels
                        ],
                        "success_visible": success_visible,
                        "failure_messages": failure_messages,
                        "challenge_visible": challenge_visible,
                        "recaptcha_control_present": recaptcha_control_present,
                        "recaptcha_token_present": recaptcha_token_present,
                        "invalid_controls": invalid_details,
                        "live_regions": live_texts,
                        "headings": heading_texts,
                    },
                    limit=1800,
                )
                if snapshot != last_snapshot:
                    logger.info(
                        "Ashby submission state attempt=%d %s",
                        attempt + 1,
                        snapshot,
                    )
                    last_snapshot = snapshot

                if outcome is not None:
                    logger.info(
                        "Ashby submission outcome status=%s detail=%s url=%s events=%s",
                        outcome.status,
                        _diagnostic_text(outcome.detail),
                        _diagnostic_url(page.url),
                        diagnostic_events,
                    )
                    return AshbySubmissionResult(
                        outcome.status,
                        page.url,
                        outcome.detail,
                    )
                if page.url != initial_url and not form_still_present:
                    logger.info(
                        "Ashby submission inferred from navigation initial_url=%s "
                        "final_url=%s events=%s",
                        _diagnostic_url(initial_url),
                        _diagnostic_url(page.url),
                        diagnostic_events,
                    )
                    return AshbySubmissionResult(
                        "submitted", page.url, "Application form completed"
                    )
            diagnostic_summary = _diagnostic_text(
                f"state={last_snapshot}; events={diagnostic_events[-8:]}",
                limit=2600,
            )
            logger.warning(
                "Ashby submission outcome remained pending after %ds: %s",
                wait_seconds,
                diagnostic_summary,
            )
            return AshbySubmissionResult(
                "manual_required",
                page.url,
                "The form did not expose a verifiable submission outcome.\n"
                f"Diagnostics: {diagnostic_summary}",
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
