"""HelloWork offer parsing, safety preflight, and authenticated submission."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from jobbot.form_answers import FormQuestion, migrate_profile_json, profile_document
from jobbot.integrations.job_page import ParsedJobPage, parse_job_html, validate_public_url
from jobbot.integrations.web_application import load_profile_data

_HOSTS = {"hellowork.com", "www.hellowork.com"}
_PATH_RE = re.compile(r"^/fr-fr/emplois/(?P<id>[0-9]+)\.html/?$")
_MANDATORY = re.compile(r"\b(obligatoire|exig[ée]e?|imp[ée]ratif|requis[ea]?|devez poss[ée]der)\b", re.I)
_OPTIONAL = re.compile(r"\b(souhait[ée]e?|un plus|id[ée]alement|appr[ée]ci[ée]e?)\b", re.I)
_QUALIFICATION = re.compile(r"\b(caces(?:\s+[a-z0-9]+)?|permis(?:\s+[a-z0-9]+)?|dipl[oô]me|certificat|habilitation)\b", re.I)
_AUTH_RE = re.compile(r"mot de passe|code de v[ée]rification|v[ée]rifiez votre identit[ée]", re.I)
_SUCCESS_RE = re.compile(r"candidature\s+(?:a\s+été\s+)?(?:envoyée|transmise)|merci\s+pour\s+votre\s+candidature", re.I)


class HelloWorkError(RuntimeError):
    def __init__(self, message: str, *, status: str = "failed"):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class HelloWorkPosting:
    page: ParsedJobPage
    offer_id: str


@dataclass(frozen=True)
class HelloWorkPreflight:
    posting: HelloWorkPosting
    missing: tuple[str, ...] = ()
    questions: tuple[FormQuestion, ...] = ()
    reused: tuple[FormQuestion, ...] = ()


@dataclass(frozen=True)
class HelloWorkSubmissionResult:
    status: str
    url: str
    detail: str = ""


def parse_hellowork_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    match = _PATH_RE.match(parsed.path)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").casefold() not in _HOSTS or not match:
        raise HelloWorkError("Unsupported HelloWork offer URL")
    offer_id = match.group("id")
    return offer_id, f"https://www.hellowork.com/fr-fr/emplois/{offer_id}.html"


def is_hellowork_job_url(url: str) -> bool:
    try:
        parse_hellowork_url(url)
        return True
    except HelloWorkError:
        return False


async def fetch_hellowork_posting(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HelloWorkPosting:
    offer_id, canonical = parse_hellowork_url(url)
    await validate_public_url(canonical)
    async def validate_request(request: httpx.Request) -> None:
        await validate_public_url(str(request.url))

    async with httpx.AsyncClient(
        timeout=20,
        transport=transport,
        follow_redirects=True,
        event_hooks={"request": [validate_request]},
    ) as client:
        response = await client.get(canonical, headers={"User-Agent": "getajob/1.0"})
        if response.status_code in {404, 410}:
            raise HelloWorkError("HelloWork offer is unavailable", status="failed")
        response.raise_for_status()
    try:
        final_offer_id, _ = parse_hellowork_url(str(response.url))
    except HelloWorkError as exc:
        raise HelloWorkError("HelloWork offer redirected to an unexpected destination") from exc
    if final_offer_id != offer_id:
        raise HelloWorkError("HelloWork offer identity changed during fetch")
    if len(response.content) > 2_000_000:
        raise HelloWorkError("HelloWork offer exceeded the safe size limit")
    page = parse_job_html(response.text, str(response.url))
    page = replace(
        page,
        source_category="hellowork_offer",
        apply_url=canonical,
        fetched_url=canonical,
        contact_kind="ats",
        contact_value="hellowork",
    )
    return HelloWorkPosting(page, offer_id)


def check_requirements(description: str, profile: dict) -> tuple[str, tuple[str, ...]]:
    facts = dict(profile.get("facts") or {})
    missing = tuple(str(item).casefold() for item in facts.get("missing_requirements") or ())
    qualifications = tuple(str(item).casefold() for item in facts.get("qualifications") or ())
    ambiguous: list[str] = []
    unmet: list[str] = []
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", description):
        if not _MANDATORY.search(sentence) or _OPTIONAL.search(sentence):
            continue
        requirement = _QUALIFICATION.search(sentence)
        if not requirement:
            continue
        label = requirement.group(0).casefold()
        if any(item in sentence.casefold() or label in item for item in missing):
            unmet.append(sentence.strip()[:240])
        elif not any(label in item or item in sentence.casefold() for item in qualifications):
            ambiguous.append(sentence.strip()[:240])
    if unmet:
        return "requirements_unmet", tuple(unmet)
    if ambiguous:
        return "requirements_ambiguous", tuple(ambiguous)
    return "ok", ()


async def preflight_hellowork_application(
    url: str,
    resume_path: Path,
    profile_path: Path,
    *,
    answer_db_path: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HelloWorkPreflight:
    if not resume_path.is_file():
        raise HelloWorkError(f"Resume is missing: {resume_path.name}", status="resume_missing")
    posting = await fetch_hellowork_posting(url, transport=transport)
    try:
        if answer_db_path is not None:
            migrate_profile_json(answer_db_path, profile_path)
            raw = profile_document(answer_db_path)
        else:
            raw = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.is_file() else {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HelloWorkError(f"Applicant profile is invalid: {exc}") from exc
    status, detail = check_requirements(posting.page.vacancy.description, raw)
    if status != "ok":
        raise HelloWorkError("; ".join(detail), status=status)
    return HelloWorkPreflight(posting)


def _profile_values(raw: dict, resume_path: Path) -> dict[str, str]:
    profile = load_profile_data(raw, resume_path)
    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    return {
        "first": profile.first_name,
        "last": profile.last_name,
        "email": profile.email,
        "phone": profile.phone,
        "address": str(raw.get("address") or location.get("address") or "").strip(),
        **{str(key): str(value) for key, value in dict(raw.get("answers") or {}).items()},
    }


async def _fill_conventional_form(
    page, values: dict[str, str], resume_path: Path, *, upload_resume: bool
) -> tuple[str, ...]:
    missing: list[str] = []
    controls = page.locator("input:visible, textarea:visible, select:visible")
    for index in range(await controls.count()):
        control = controls.nth(index)
        if await control.is_disabled():
            continue
        kind = (await control.get_attribute("type") or await control.evaluate("e => e.tagName")).casefold()
        if kind in {"hidden", "submit", "button", "reset"}:
            continue
        if kind == "file":
            if upload_resume:
                await control.set_input_files(str(resume_path))
            continue
        name = " ".join(filter(None, [
            await control.get_attribute("name"), await control.get_attribute("id"),
            await control.get_attribute("placeholder"), await control.get_attribute("aria-label"),
        ])).casefold()
        value = ""
        matched_key = ""
        for key, candidate in values.items():
            aliases = {
                "first": ("first", "prenom", "prénom"), "last": ("last", "surname", "nom"),
                "email": ("email", "mail"), "phone": ("phone", "tel", "mobile"),
                "address": ("address", "adresse"),
            }.get(key, (key.casefold(),))
            if any(alias in name for alias in aliases):
                value = candidate
                matched_key = key
                break
        if kind in {"checkbox", "radio"}:
            checked = await control.is_checked()
            option_value = (await control.get_attribute("value") or "on").casefold()
            if value and (
                value.casefold() in {"1", "true", "yes", "oui", "on", option_value}
            ):
                await control.check()
                checked = True
            if await control.get_attribute("required") is not None and not checked:
                missing.append(await control.get_attribute("name") or "required choice")
            continue
        current = await control.input_value()
        should_replace = matched_key in {"first", "last", "email", "phone", "address"}
        if value and (not current or (should_replace and current.strip() != value.strip())):
            if (await control.evaluate("e => e.tagName")).casefold() == "select":
                try:
                    await control.select_option(label=value)
                except Exception:
                    await control.select_option(value=value)
            else:
                await control.fill(value)
        elif await control.get_attribute("required") is not None and not current:
            missing.append(name or "required field")
    return tuple(dict.fromkeys(missing))


async def submit_hellowork_application(
    url: str,
    resume_path: Path,
    profile_path: Path,
    auth_state_path: Path,
    *,
    headless: bool = True,
    answer_db_path: Path | None = None,
) -> HelloWorkSubmissionResult:
    canonical = parse_hellowork_url(url)[1]
    if not auth_state_path.is_file():
        return HelloWorkSubmissionResult("auth_required", canonical, "HelloWork authentication state is missing")
    try:
        if answer_db_path is not None:
            migrate_profile_json(answer_db_path, profile_path)
            raw = profile_document(answer_db_path)
        else:
            raw = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.is_file() else {}
        values = _profile_values(raw, resume_path)
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=headless)
            context = await browser.new_context(storage_state=str(auth_state_path))
            page = await context.new_page()
            await page.goto(canonical, wait_until="domcontentloaded", timeout=45_000)
            body = await page.locator("body").inner_text()
            if (
                _AUTH_RE.search(body)
                or "/connexion" in page.url
                or await page.locator('input[type="password"]:visible').count()
            ):
                await browser.close()
                return HelloWorkSubmissionResult("auth_required", page.url, "HelloWork login or verification is required")
            challenge = page.locator('iframe[src*="captcha" i], iframe[title*="challenge" i], [class*="captcha" i]')
            if await challenge.count():
                await browser.close()
                return HelloWorkSubmissionResult("auth_required", page.url, "HelloWork CAPTCHA requires attention")
            apply = page.get_by_role("button", name=re.compile(r"^postuler", re.I)).or_(
                page.get_by_role("link", name=re.compile(r"^postuler", re.I))
            )
            if not await apply.count():
                await browser.close()
                return HelloWorkSubmissionResult("failed", page.url, "Offer is unavailable or already applied")
            await apply.first.click()
            await page.wait_for_timeout(800)
            if urlparse(page.url).scheme != "https":
                await browser.close()
                return HelloWorkSubmissionResult("external_form_unsupported", page.url, "Unsafe recruiter redirect")
            await validate_public_url(page.url)
            post_click_body = await page.locator("body").inner_text()
            if (
                _AUTH_RE.search(post_click_body)
                or await page.locator('input[type="password"]:visible').count()
            ):
                await browser.close()
                return HelloWorkSubmissionResult("auth_required", page.url, "Recruiter authentication is required")
            if await page.locator(
                'iframe[src*="captcha" i], iframe[title*="challenge" i], [class*="captcha" i]'
            ).count():
                await browser.close()
                return HelloWorkSubmissionResult("external_form_unsupported", page.url, "Recruiter CAPTCHA requires attention")
            external = (urlparse(page.url).hostname or "").casefold() not in _HOSTS
            missing = await _fill_conventional_form(
                page, values, resume_path, upload_resume=external
            )
            if missing:
                await context.storage_state(path=str(auth_state_path))
                await browser.close()
                return HelloWorkSubmissionResult("answers_required", page.url, ", ".join(missing[:8]))
            submit = page.get_by_role("button", name=re.compile(r"envoyer|soumettre|postuler|candidater", re.I))
            if not await submit.count():
                await browser.close()
                return HelloWorkSubmissionResult("external_form_unsupported", page.url, "No verified submit control")
            await submit.last.click()
            await page.wait_for_timeout(1200)
            result_text = await page.locator("body").inner_text()
            await context.storage_state(path=str(auth_state_path))
            result_url = page.url
            await browser.close()
            if _SUCCESS_RE.search(result_text):
                return HelloWorkSubmissionResult("submitted", result_url, "confirmed")
            return HelloWorkSubmissionResult("submission_unknown", result_url, "No explicit success confirmation")
    except HelloWorkError:
        raise
    except Exception as exc:
        raise HelloWorkError(f"HelloWork browser submission failed: {type(exc).__name__}: {exc}") from exc
