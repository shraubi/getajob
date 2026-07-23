"""Greenhouse job-board adapter with API preflight and browser submission."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from jobbot.application import Vacancy
from jobbot.form_answers import (
    FormQuestion,
    SKIPPED,
    classify_question,
    get_fact,
    migrate_profile_json,
    profile_document,
)
from jobbot.integrations.form_matching import best_field_label_match, best_submit_control_match, classify_form_submission
from jobbot.integrations.job_page import ParsedJobPage, validate_public_url
from jobbot.integrations.web_application import load_profile, load_profile_data

_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true"
_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}


class GreenhouseError(RuntimeError):
    pass


@dataclass(frozen=True)
class GreenhouseField:
    name: str
    label: str
    field_type: str
    required: bool
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class GreenhousePosting:
    page: ParsedJobPage
    board: str
    job_id: int
    fields: tuple[GreenhouseField, ...]


@dataclass(frozen=True)
class GreenhousePreflight:
    posting: GreenhousePosting
    submissions: dict[str, object]
    missing: tuple[str, ...]
    questions: tuple[FormQuestion, ...] = ()
    reused: tuple[FormQuestion, ...] = ()


@dataclass(frozen=True)
class GreenhouseSubmissionResult:
    status: str
    url: str
    detail: str = ""


BrowserSubmitter = Callable[[GreenhousePreflight, Path, Path, bool], Awaitable[GreenhouseSubmissionResult]]


def parse_greenhouse_url(url: str) -> tuple[str, int, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    board = ""
    job_id = ""
    if host in _HOSTS and len(parts) >= 3 and parts[-2] == "jobs":
        board, job_id = parts[0], parts[-1]
    elif host in _HOSTS and parts[:2] == ["embed", "job_app"]:
        query = parse_qs(parsed.query)
        board = (query.get("for") or [""])[0]
        job_id = (query.get("token") or [""])[0]
    if not board or not job_id.isdigit():
        raise GreenhouseError("Unsupported Greenhouse job URL")
    canonical = f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}"
    return board, int(job_id), canonical


def is_greenhouse_job_url(url: str) -> bool:
    try:
        parse_greenhouse_url(url)
        return True
    except GreenhouseError:
        return False


def _fields(payload: dict) -> tuple[GreenhouseField, ...]:
    result: list[GreenhouseField] = []
    for question in payload.get("questions") or ():
        label = BeautifulSoup(str(question.get("label") or ""), "html.parser").get_text(" ", strip=True)
        for field in question.get("fields") or ():
            name = str(field.get("name") or "").strip()
            if not name:
                continue
            values = field.get("values") or ()
            options = tuple(str(item.get("label") if isinstance(item, dict) else item) for item in values)
            result.append(GreenhouseField(name, label or name, str(field.get("type") or "input_text"), bool(question.get("required")), options))
    return tuple(result)


async def fetch_greenhouse_posting(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> GreenhousePosting:
    board, job_id, canonical = parse_greenhouse_url(url)
    await validate_public_url(canonical)
    async with httpx.AsyncClient(timeout=20.0, transport=transport) as client:
        response = await client.get(_API.format(board=board, job_id=job_id))
        response.raise_for_status()
        payload = response.json()
    title = str(payload.get("title") or "").strip()
    description = BeautifulSoup(str(payload.get("content") or ""), "html.parser").get_text("\n", strip=True)
    if not title or len(description) < 40:
        raise GreenhouseError("Greenhouse did not expose enough vacancy information")
    company = str(payload.get("company_name") or board).strip()
    location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    vacancy = Vacancy(title[:160], company[:120], description[:20_000], canonical, "ats", location=str(location.get("name") or ""))
    page = ParsedJobPage(vacancy, "greenhouse_application_form", canonical, canonical, "ats", "greenhouse")
    fields = _fields(payload)
    if not fields:
        raise GreenhouseError("Greenhouse vacancy has no public application form contract")
    return GreenhousePosting(page, board, job_id, fields)


def _profile_value(field: GreenhouseField, profile, raw: dict):
    key = f"{field.name} {field.label}".casefold()
    answers = raw.get("answers") if isinstance(raw.get("answers"), dict) else {}
    if field.name in answers:
        return answers[field.name]
    for answer_key, value in answers.items():
        if str(answer_key).casefold() == field.label.casefold():
            return value
    if "first" in key and "name" in key:
        return profile.first_name
    if "last" in key and "name" in key:
        return profile.last_name
    if "email" in key:
        return profile.email
    if "phone" in key:
        return profile.phone
    if "resume" in key or field.field_type == "input_file":
        return "__resume__"
    links = raw.get("links") if isinstance(raw.get("links"), dict) else {}
    for link in ("linkedin", "github", "portfolio", "website"):
        if link in key:
            return links.get(link)
    return None


async def preflight_greenhouse_application(
    url: str,
    resume_path: Path,
    profile_path: Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    answer_db_path: Path | None = None,
) -> GreenhousePreflight:
    if not resume_path.is_file():
        raise GreenhouseError(f"Resume is missing: {resume_path.name}")
    try:
        if answer_db_path is not None:
            migrate_profile_json(answer_db_path, profile_path)
            raw = profile_document(answer_db_path)
            profile = load_profile_data(raw, resume_path)
        else:
            raw = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.is_file() else {}
            profile = load_profile(profile_path, resume_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise GreenhouseError(f"Applicant profile is invalid: {exc}") from exc
    posting = await fetch_greenhouse_posting(url, transport=transport)
    submissions: dict[str, object] = {}
    missing: list[str] = []
    questions: list[FormQuestion] = []
    reused: list[FormQuestion] = []
    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    context = {
        "job_id": str(posting.job_id),
        "company": posting.page.vacancy.company,
        "country": str(location.get("country") or ""),
        "job_country": posting.page.vacancy.location,
    }
    for field in posting.fields:
        question = classify_question(
            "greenhouse", field.name, field.label, field.field_type,
            field.options, field.required, context=context,
        )
        resolution = get_fact(answer_db_path, question) if answer_db_path is not None else None
        if resolution and resolution.form_value == SKIPPED:
            continue
        if resolution:
            reused.append(question)
        value = resolution.form_value if resolution else _profile_value(field, profile, raw)
        if value in (None, "", []):
            if field.required:
                missing.append(f"{field.label} [{field.name}]")
                questions.append(question)
            elif field.options or "select" in field.field_type.casefold():
                questions.append(question)
            continue
        submissions[field.name] = value
    return GreenhousePreflight(
        posting, submissions, tuple(missing), tuple(questions), tuple(reused)
    )


def format_missing_questions(preflight: GreenhousePreflight) -> str:
    return "" if not preflight.missing else "Required Greenhouse answers are missing:\n" + "\n".join(f"• {item}" for item in preflight.missing)


async def _submit_with_playwright(preflight: GreenhousePreflight, resume_path: Path, browser_profile_path: Path, headless: bool) -> GreenhouseSubmissionResult:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise GreenhouseError("Playwright is not installed for Greenhouse browser submission") from exc
    browser_profile_path.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(str(browser_profile_path), headless=headless)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(preflight.posting.page.apply_url, wait_until="domcontentloaded", timeout=30_000)
            labels = page.locator("label")
            label_texts = await labels.all_inner_texts()
            for field in preflight.posting.fields:
                if field.name not in preflight.submissions:
                    continue
                value = preflight.submissions[field.name]
                control = page.locator(f'[name={json.dumps(field.name)}]')
                if await control.count() == 0:
                    index = best_field_label_match(field.label, label_texts)
                    if index is not None:
                        label = labels.nth(index)
                        target = await label.get_attribute("for")
                        if target:
                            control = page.locator(f'[id={json.dumps(target)}]')
                if await control.count() == 0:
                    raise GreenhouseError(f"Could not locate form field: {field.label}")
                control = control.first
                if value == "__resume__":
                    await control.set_input_files(str(resume_path))
                elif field.field_type in {"input_file"}:
                    await control.set_input_files(str(resume_path))
                elif await control.evaluate("(element) => element.tagName === 'SELECT'"):
                    await control.select_option(label=str(value))
                elif (await control.get_attribute("type") or "").casefold() in {"radio", "checkbox"}:
                    await control.check()
                else:
                    await control.fill(str(value))
            controls = page.locator('button:visible, input[type="submit"]:visible')
            texts = await controls.evaluate_all("(els) => els.map(e => (e.innerText || e.value || '').trim())")
            index = best_submit_control_match(texts)
            if index is None:
                raise GreenhouseError(f"Could not identify a unique submit control (visible controls={texts})")
            initial_url = page.url
            await controls.nth(index).click()
            for _ in range(60):
                await page.wait_for_timeout(500)
                success = page.locator('[data-provided-by="greenhouse"] .confirmation, .application-confirmation, #application_confirmation')
                failure = page.locator('[role="alert"], .field-error, .error-message')
                challenge = page.locator('iframe[title*="challenge" i], iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i]')
                success_visible = await success.count() > 0 and await success.first.is_visible()
                failure_visible = await failure.count() > 0 and await failure.first.is_visible()
                challenge_visible = await challenge.count() > 0 and await challenge.first.is_visible()
                form_visible = await page.locator('form input[type="file"]').count() > 0
                if (page.url != initial_url or not form_visible) and not failure_visible and not challenge_visible:
                    return GreenhouseSubmissionResult("submitted", page.url, "Application form completed")
                outcome = classify_form_submission(
                    success_present=success_visible,
                    success_text=(await success.first.inner_text()) if success_visible else "",
                    failure_present=failure_visible,
                    failure_text=(await failure.first.inner_text()) if failure_visible else "",
                    challenge_present=challenge_visible,
                    challenge_text="Interactive verification is required",
                )
                if outcome:
                    return GreenhouseSubmissionResult(outcome.status, page.url, outcome.detail)
            return GreenhouseSubmissionResult("manual_required", page.url, "The form did not expose a verifiable submission outcome")
        finally:
            await context.close()


async def submit_greenhouse_application(
    url: str,
    resume_path: Path,
    profile_path: Path,
    browser_profile_path: Path,
    *,
    headless: bool = True,
    browser_submitter: BrowserSubmitter | None = None,
    answer_db_path: Path | None = None,
) -> GreenhouseSubmissionResult:
    preflight = await preflight_greenhouse_application(
        url, resume_path, profile_path, answer_db_path=answer_db_path
    )
    if preflight.missing:
        raise GreenhouseError(format_missing_questions(preflight))
    return await (browser_submitter or _submit_with_playwright)(preflight, resume_path, browser_profile_path, headless)

