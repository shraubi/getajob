"""Deterministic rating chain for one live job URL."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from classifier import classify
from job_page import ParsedJobPage, fetch_job_from_message


@dataclass(frozen=True)
class StageRating:
    stage: str
    passed: bool
    points: int
    possible: int
    summary: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class RatingReport:
    url: str
    domain: str
    title: str
    company: str
    direction: str
    score: int
    status: str
    stages: tuple[StageRating, ...]

    @property
    def failures(self) -> tuple[StageRating, ...]:
        return tuple(stage for stage in self.stages if not stage.passed)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _parse_rating(page: ParsedJobPage) -> StageRating:
    missing: list[str] = []
    if not page.vacancy.title.strip():
        missing.append("title")
    if not page.vacancy.company.strip() or page.vacancy.company == "Unknown company":
        missing.append("company")
    if len(page.vacancy.description.strip()) < 80:
        missing.append("description")
    return StageRating(
        stage="parser",
        passed=not missing,
        points=40 if not missing else 0,
        possible=40,
        summary="Required vacancy fields were parsed" if not missing else "Missing or weak vacancy fields: " + ", ".join(missing),
        evidence={
            "source_category": page.source_category,
            "fetched_url": page.fetched_url,
            "description_length": len(page.vacancy.description),
            "missing": missing,
        },
    )


def _classification_rating(page: ParsedJobPage, direction: str, expected_direction: str | None) -> StageRating:
    expected = expected_direction or "a supported direction"
    passed = direction == expected_direction if expected_direction else direction != "other"
    return StageRating(
        stage="classification",
        passed=passed,
        points=30 if passed else 0,
        possible=30,
        summary=f"Classified as {direction}" if passed else f"Expected {expected}, classified as {direction}",
        evidence={"actual_direction": direction, "expected_direction": expected_direction},
    )


def _application_rating(page: ParsedJobPage) -> StageRating:
    host = (urlparse(page.fetched_url).hostname or "").casefold()
    same_page_target = bool(page.apply_url) and page.apply_url == page.fetched_url
    static_form = page.source_category == "application_form"
    dedicated_adapter = ""
    if host in {"hirify.me", "www.hirify.me"}:
        dedicated_adapter = "hirify_contact_or_direct_application"
    elif host == "getmatch.ru" or host.endswith(".getmatch.ru"):
        dedicated_adapter = "getmatch_auth_blocker"
    elif host == "koronatech.ru" or host.endswith(".koronatech.ru"):
        dedicated_adapter = "koronatech_captcha_blocker"
    passed = bool(dedicated_adapter) or (bool(page.apply_url) and (static_form or not same_page_target))
    if dedicated_adapter:
        summary = f"Application behavior is handled by {dedicated_adapter}"
    elif not page.apply_url:
        summary = "No application target was discovered"
    elif same_page_target and not static_form:
        summary = "Application target is a same-page JavaScript flow with no discoverable static form"
    else:
        summary = "Application target is available for preflight"
    return StageRating(
        stage="application",
        passed=passed,
        points=30 if passed else 0,
        possible=30,
        summary=summary,
        evidence={
            "apply_url": page.apply_url,
            "fetched_url": page.fetched_url,
            "source_category": page.source_category,
            "same_page_target": same_page_target,
            "static_form": static_form,
            "dedicated_adapter": dedicated_adapter,
        },
    )


async def rate_job(url: str, *, expected_direction: str | None = None) -> RatingReport:
    page = await fetch_job_from_message(url)
    direction = classify(page.vacancy.title, page.vacancy.description)
    stages = (
        _parse_rating(page),
        _classification_rating(page, direction, expected_direction),
        _application_rating(page),
    )
    score = sum(stage.points for stage in stages)
    return RatingReport(
        url=url,
        domain=(urlparse(page.fetched_url).hostname or "unknown").casefold(),
        title=page.vacancy.title,
        company=page.vacancy.company,
        direction=direction,
        score=score,
        status="passed" if all(stage.passed for stage in stages) else "failed",
        stages=stages,
    )


def failure_fingerprint(report: RatingReport, failure: StageRating) -> str:
    payload = json.dumps(
        {"domain": report.domain, "stage": failure.stage, "summary": failure.summary},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

