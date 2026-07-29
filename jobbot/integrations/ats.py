"""Provider-neutral facade for browser-assisted ATS applications."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from jobbot.form_answers import FormQuestion
from jobbot.integrations.ashby import (
    AshbyError,
    fetch_ashby_posting,
    format_missing_questions as format_ashby_missing,
    is_ashby_job_url,
    preflight_ashby_application,
    submit_ashby_application,
)
from jobbot.integrations.greenhouse import (
    GreenhouseError,
    fetch_greenhouse_posting,
    format_missing_questions as format_greenhouse_missing,
    is_greenhouse_job_url,
    preflight_greenhouse_application,
    submit_greenhouse_application,
)
from jobbot.integrations.hellowork import (
    HelloWorkError,
    fetch_hellowork_posting,
    is_hellowork_job_url,
    preflight_hellowork_application,
    submit_hellowork_application,
)
from jobbot.integrations.job_page import ParsedJobPage


class AtsError(RuntimeError):
    def __init__(self, message: str, *, status: str = "failed"):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AtsPreflight:
    provider: str
    page: ParsedJobPage
    missing: tuple[str, ...]
    native: object
    questions: tuple[FormQuestion, ...] = ()
    reused: tuple[FormQuestion, ...] = ()


@dataclass(frozen=True)
class AtsSubmissionResult:
    status: str
    url: str
    detail: str = ""


def ats_provider(url: str) -> str:
    if is_hellowork_job_url(url):
        return "hellowork"
    if is_ashby_job_url(url):
        return "ashby"
    if is_greenhouse_job_url(url):
        return "greenhouse"
    return ""


def is_ats_job_url(url: str) -> bool:
    return bool(ats_provider(url))


async def fetch_ats_page(url: str) -> ParsedJobPage:
    provider = ats_provider(url)
    try:
        if provider == "ashby":
            page = (await fetch_ashby_posting(url)).page
        elif provider == "greenhouse":
            page = (await fetch_greenhouse_posting(url)).page
        elif provider == "hellowork":
            page = (await fetch_hellowork_posting(url)).page
        else:
            raise AtsError("Unsupported ATS application URL")
    except (AshbyError, GreenhouseError, HelloWorkError) as exc:
        raise AtsError(str(exc), status=getattr(exc, "status", "failed")) from exc
    return replace(page, contact_kind="ats", contact_value=provider)


async def preflight_ats_application(
    url: str,
    resume_path: Path,
    profile_path: Path,
    *,
    answer_db_path: Path | None = None,
) -> AtsPreflight:
    provider = ats_provider(url)
    try:
        if provider == "ashby":
            native = await preflight_ashby_application(
                url, resume_path, profile_path, answer_db_path=answer_db_path
            )
        elif provider == "greenhouse":
            native = await preflight_greenhouse_application(
                url, resume_path, profile_path, answer_db_path=answer_db_path
            )
        elif provider == "hellowork":
            native = await preflight_hellowork_application(
                url, resume_path, profile_path, answer_db_path=answer_db_path
            )
        else:
            raise AtsError("Unsupported ATS application URL")
    except (AshbyError, GreenhouseError, HelloWorkError) as exc:
        raise AtsError(str(exc), status=getattr(exc, "status", "failed")) from exc
    page = replace(native.posting.page, contact_kind="ats", contact_value=provider)
    return AtsPreflight(
        provider, page, native.missing, native, native.questions, native.reused
    )


def format_missing_questions(preflight: AtsPreflight) -> str:
    if preflight.provider == "ashby":
        return format_ashby_missing(preflight.native)
    if preflight.provider == "greenhouse":
        return format_greenhouse_missing(preflight.native)
    return "HelloWork needs additional application information."


async def submit_ats_application(
    url: str,
    resume_path: Path,
    profile_path: Path,
    browser_profile_path: Path,
    *,
    headless: bool = True,
    answer_db_path: Path | None = None,
) -> AtsSubmissionResult:
    provider = ats_provider(url)
    try:
        if provider == "ashby":
            result = await submit_ashby_application(
                url, resume_path, profile_path, browser_profile_path,
                headless=headless, answer_db_path=answer_db_path,
            )
        elif provider == "greenhouse":
            result = await submit_greenhouse_application(
                url, resume_path, profile_path, browser_profile_path,
                headless=headless, answer_db_path=answer_db_path,
            )
        elif provider == "hellowork":
            result = await submit_hellowork_application(
                url, resume_path, profile_path, browser_profile_path,
                headless=headless, answer_db_path=answer_db_path,
            )
        else:
            raise AtsError("Unsupported ATS application URL")
    except (AshbyError, GreenhouseError, HelloWorkError) as exc:
        raise AtsError(str(exc), status=getattr(exc, "status", "failed")) from exc
    return AtsSubmissionResult(result.status, result.url, result.detail)

