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
            supplied = vuÛÎ½¶‰ËkºwµçW7–æ2FVbÇ•öF—&V7B‡6VÆbÂf6æ7•ö–B“ Ğ¢6VÆbæÆ–VBæVæB‡f6æ7•ö–BĞ¢&WGW&â““Ğ Ğ Ğ¦6Æ72f¶U6VæFW# Ğ¢6ÆÇ2ÒµĞĞ Ğ¢FVbõö–æ—Eõò‡6VÆbÂ¥ö&w2“ Ğ¢70Ğ Ğ¢7–æ2FVb6VæE÷&W7VÖR‡6VÆbÂW6W&æÖRÂÖW76vRÂ&W7VÖU÷F‚“ Ğ¢6VÆbæ6ÆÇ2æVæB‚‡W6W&æÖRÂÖW76vRÂ&W7VÖU÷F‚ææÖR’Ğ¢&WGW&âC#C Ğ Ğ Ğ¦6Æ72f¶UVW'“ Ğ¢FVbõö–æ—Eõò‡6VÆbÂFF“ Ğ¢6VÆbæFFÒFFĞ¢6VÆbæVF—FVBÒ" Ğ Ğ¢7–æ2FVbç7vW"‡6VÆb“ Ğ¢70Ğ Ğ¢7–æ2FVbVF—EöÖW76vU÷FW‡B‡6VÆbÂFW‡B“ Ğ¢6VÆbæVF—FVBÒFW‡@Ğ Ğ Ğ¦6Æ72&÷DÆ–6F–öäfÆ÷uFW7G2‡Væ—GFW7Bä—6öÆFVD7–æ6–õFW7D66R“ Ğ¢7–æ2FVbFW7EöVÖ–Åö6öçF7Eö†5ö7F–öåö'WGFöâ‡6VÆb“ Ğ¢W&ÂÒ&‡GG3¢òö†—&–g’æÖRö¦ö'2ósC3C2Ö’ÖVæv–æVW"ÖÆ–VBÖ’ÖVæv–æVW"×—F†öâ Ğ¢f6æ7’Òf6æ7’€Ğ¢F—FÆSÒ$’Væv–æVW""Â6ö×ç“Ò$W†×ÆR"ÀĞ¢FW67&—F–öãÒ$’Væv–æVW"—F†öâÄÄÒ&öÆR"¢RÂW&Ã×W&ÂÀĞ¢Ğ¢vRÒ'6VD¦ö%vR‡f6æ7’Â'7G'V7GW&VEö¦ö%÷vR"Â""ÂW&ÂĞ¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2F—&V7F÷'“ Ğ¢&ö÷BÒF‚†F—&V7F÷'’Ğ¢&W7VÖRÒ&ö÷Bò&’çFb Ğ¢&W7VÖRçw&—FUö'—FW2†"'Fb"Ğ¢&÷BÒf¶T&÷B‚Ğ¢G&gBÒÆ–6F–öäG&gB‡f6æ7’Â&ÖÅöVæv–æVW&–ær"Â&W7VÖRÂ""Ğ¢v—F‚€Ğ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æfWF6…ö¦ö%ög&öÕöÖW76vR"ÂæWsÔ7–æ4Öö6²‡&WGW&å÷fÇVS×vR’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2åövWEö†—&–g•ö6Æ–VçB"Â&WGW&å÷fÇVSÔf¶TVÖ–Ä†—&–g”6Æ–VçB‚’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æ'V–ÆEöÆ–6F–öåöf÷%÷f6æ7’"Â&WGW&å÷fÇVSÖG&gB’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$¤ô%5ôD%õD‚"Â&ö÷Bò&¦ö'2æF""’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ%$U5TÔUôD•""Â&ö÷B’ÀĞ¢“ Ğ¢v—Bö†æFÆU÷Fö¶Våög&VR…6–×ÆTæÖW76R†&÷CÖ&÷B’ÂW&ÂĞ¢'WGFöâÒ&÷BæÖW76vW5²ÓÕ²'&WÇ•öÖ&·W%Òæ–æÆ–æUö¶W–&ö&E³Õ³ĞĞ¢6VÆbæ76W'DWVÂ†'WGFöâçW&ÂÂ&Ö–ÇFó¦¦ö'4W†×ÆRæ6öÒ"Ğ¢6VÆbæ76W'D–â‚$6öçF7C¢¦ö'4W†×ÆRæ6öÒ"Â&÷BæÖW76vW5³Õ²'FW‡B%ÒĞ Ğ¢7–æ2FVbFW7EöG5÷F&vWE÷&W6W'fW5ö†—&–g•÷f6æ7•öf÷%ö6Æ76–f–6F–öâ‡6VÆb“ Ğ¢W&ÂÒ&‡GG3¢òö†—&–g’æÖRö¦ö'2ós3ScB×6Væ–÷"Ög&öçFVæBÖVæv–æVW"×vV#2 Ğ¢6÷W&6U÷f6æ7’Òf6æ7’€Ğ¢F—FÆSÒ%6Væ–÷"g&öçFVæBVæv–æVW"vV#2"Â6ö×ç“Ò$W†×ÆR"ÀĞ¢FW67&—F–öãÒ%—F†öâ&6¶VæBÆFf÷&ÒVæv–æVW&–ær&öÆR"¢RÂW&Ã×W&ÂÀĞ¢Ğ¢6÷W&6U÷vRÒ'6VD¦ö%vR‡6÷W&6U÷f6æ7’Â'7G'V7GW&VEö¦ö%÷vR"Â""ÂW&ÂĞ¢F&vWE÷W&ÂÒ&‡GG3¢òö¦ö"Ö&ö&G2æw&VVæ†÷W6Ræ–òöW†×ÆRö¦ö'2ós3ScB Ğ¢F&vWE÷f6æ7’Òf6æ7’€Ğ¢F—FÆSÒ%7"6ögGv&RVæv–æVW"Âg&öçBVæB"Â6ö×ç“Ò$W†×ÆR"ÀĞ¢FW67&—F–öãÒ$g&öçFVæB&öGV7B&öÆR"¢RÂW&Ã×F&vWE÷W&ÂÀĞ¢Ğ¢G5÷vRÒ'6VD¦ö%vR€Ğ¢F&vWE÷f6æ7’Â&w&VVæ†÷W6UöÆ–6F–öåöf÷&Ò"ÂF&vWE÷W&ÂÂF&vWE÷W&ÂÀĞ¢6öçF7Eö¶–æCÒ&G2"Â6öçF7E÷fÇVSÒ&w&VVæ†÷W6R"ÀĞ¢Ğ¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2F—&V7F÷'“ Ğ¢&ö÷BÒF‚†F—&V7F÷'’Ğ¢&W7VÖRÒ&ö÷Bò&&6¶VæBçFb Ğ¢&W7VÖRçw&—FUö'—FW2†"'Fb"Ğ¢&÷BÒf¶T&÷B‚Ğ¢G&gBÒÆ–6F–öäG&gB‡6÷W&6U÷f6æ7’Â&&6¶VæE÷—F†öâ"Â&W7VÖRÂ""Ğ¢'V–ÆFW"ÒVæ—GFW7BæÖö6²äÖö6²‡&WGW&å÷fÇVSÖG&gBĞ¢&VfÆ–v‡BÒG5&VfÆ–v‡B‚&w&VVæ†÷W6R"ÂG5÷vRÂ‚’Âö&¦V7B‚’Ğ¢v—F‚€Ğ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æfWF6…ö¦ö%ög&öÕöÖW76vR"ÂæWsÔ7–æ4Öö6²‡&WGW&å÷fÇVS×6÷W&6U÷vR’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2åövWEö†—&–g•ö6Æ–VçB"Â&WGW&å÷fÇVSÔf¶UW&Ä†—&–g”6Æ–VçB‚’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2ç&W6öÇfUöÆ–6F–öå÷W&Â"ÂæWsÔ7–æ4Öö6²‡&WGW&å÷fÇVS×F&vWE÷W&Â’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æfWF6…öG5÷vR"ÂæWsÔ7–æ4Öö6²‡&WGW&å÷fÇVSÖG5÷vR’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2ç&VfÆ–v‡EöG5öÆ–6F–öâ"ÂæWsÔ7–æ4Öö6²‡&WGW&å÷fÇVS×&VfÆ–v‡B’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æ'V–ÆEöÆ–6F–öåöf÷%÷f6æ7’"Â'V–ÆFW"’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$¤ô%5ôD%õD‚"Â&ö÷Bò&¦ö'2æF""’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ%$U5TÔUôD•""Â&ö÷B’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$Ä”4D”ôåõ$ôd”ÄUõD‚"Â&ö÷Bò&Æ–6çBæ§6öâ"’ÀĞ¢“ Ğ¢v—Bö†æFÆU÷Fö¶Våög&VR…6–×ÆTæÖW76R†&÷CÖ&÷B’ÂW&ÂĞ¢6VÆbæ76W'D—2†'V–ÆFW"æ6ÆÅö&w2æ&w5³ÒÂ6÷W&6U÷f6æ7’Ğ¢6VÆbæ76W'EG'VR€Ğ¢&÷BæÖW76vW5²ÓÕ²'&WÇ•öÖ&·W%Òæ–æÆ–æUö¶W–&ö&E³Õ³Òæ6ÆÆ&6µöFFç7F'G7v—F‚‚&G6Ç“¢"Ğ¢Ğ Ğ¢7–æ2FVbFW7Eö†—&–g•öF—&V7EöÆ–6F–öåööæUö'WGFöåöG'•÷'Vâ‡6VÆb“ Ğ¢W&ÂÒ&‡GG3¢òö†—&–g’æÖRö¦ö'2ós3#ƒ×—F†öâÖFWfVÆ÷W" Ğ¢f6æ7’Òf6æ7’€Ğ¢F—FÆSÒ%—F†öâFWfVÆ÷W""Â6ö×ç“Ò$$UD%’"ÀĞ¢FW67&—F–öãÒ%—F†öâ7–æ6–ò&6¶VæBÖ–7&÷6W'f–6W2&öÆR"¢RÂW&Ã×W&ÂÀĞ¢Ğ¢vRÒ'6VD¦ö%vR‡f6æ7’Â'7G'V7GW&VEö¦ö%÷vR"Â""ÂW&ÂĞ¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2F—&V7F÷'“ Ğ¢&ö÷BÒF‚†F—&V7F÷'’Ğ¢&W7VÖRÒ&ö÷Bò&&6¶VæBçFb Ğ¢&W7VÖRçw&—FUö'—FW2†"'Fb"Ğ¢F%÷F‚Ò&ö÷Bò&¦ö'2æF" Ğ¢&÷BÒf¶T&÷B‚Ğ¢6Æ–VçBÒf¶TF—&V7D†—&–g”6Æ–VçB‚Ğ¢6Æ–VçBæÆ–VBæ6ÆV"‚Ğ¢G&gBÒÆ–6F–öäG&gB‡f6æ7’Â&&6¶VæE÷—F†öâ"Â&W7VÖRÂ""Ğ¢v—F‚€Ğ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æfWF6…ö¦ö%ög&öÕöÖW76vR"ÂæWsÔ7–æ4Öö6²‡&WGW&å÷fÇVS×vR’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2åövWEö†—&–g•ö6Æ–VçB"Â&WGW&å÷fÇVSÖ6Æ–VçB’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æ'V–ÆEöÆ–6F–öåöf÷%÷f6æ7’"Â&WGW&å÷fÇVSÖG&gB’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$¤ô%5ôD%õD‚"ÂF%÷F‚’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ%$U5TÔUôD•""Â&ö÷B’ÀĞ¢“ Ğ¢v—Bö†æFÆU÷Fö¶Våög&VR…6–×ÆTæÖW76R†&÷CÖ&÷B’ÂW&ÂĞ Ğ¢'WGFöåöFFÒ&÷BæÖW76vW5²ÓÕ²'&WÇ•öÖ&·W%Òæ–æÆ–æUö¶W–&ö&E³Õ³Òæ6ÆÆ&6µöFFĞ¢6VÆbæ76W'EG'VR†'WGFöåöFFç7F'G7v—F‚‚&†—&–g–Ç“¢"’Ğ¢VW'’Òf¶UVW'’†'WGFöåöFFĞ¢v—F‚€Ğ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2åövWEö†—&–g•ö6Æ–VçB"Â&WGW&å÷fÇVSÖ6Æ–VçB’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$¤ô%5ôD%õD‚"ÂF%÷F‚’ÀĞ¢“ Ğ¢v—B†æFÆUö6ÆÆ&6²…6–×ÆTæÖW76R†6ÆÆ&6µ÷VW'“×VW'’’Â6–×ÆTæÖW76R‚’Ğ¢6VÆbæ76W'DWVÂ†6Æ–VçBæÆ–VBÂ³s3#ƒÒĞ¢6VÆbæ76W'D–â‚$Æ–VBF‡&÷Vv‚†—&–g’"ÂVW'’æVF—FVBĞ Ğ¢7–æ2FVbFW7EöW‡FW&æÅöf÷&ÕööæUö'WGFöå÷7V&Ö—EöG'•÷'Vâ‡6VÆb“ Ğ¢W&ÂÒ&‡GG3¢ò÷wwræ¦ö'÷7F–ærç&òöV×Æö’Ó#cCSs‚Ó““’ Ğ¢f6æ7’Òf6æ7’€Ğ¢F—FÆSÒ%6Væ–÷"’Væv–æVW""Â6ö×ç“Ò$Æv÷FWVR"ÀĞ¢FW67&—F–öãÒ$’—F†öâÄÄÒVæv–æVW&–ær&öÆR"¢RÂW&Ã×W&ÂÀĞ¢Ğ¢vRÒ'6VD¦ö%vR‡f6æ7’Â&Æ–6F–öåöf÷&Ò"ÂW&ÂÂW&ÂĞ¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2F—&V7F÷'“ Ğ¢&ö÷BÒF‚†F—&V7F÷'’Ğ¢&W7VÖRÒ&ö÷Bò&’çFb Ğ¢&W7VÖRçw&—FUö'—FW2†"'Fb"Ğ¢F%÷F‚Ò&ö÷Bò&¦ö'2æF" Ğ¢&öf–ÆU÷F‚Ò&ö÷Bò&Æ–6çBæ§6öâ Ğ¢&öf–ÆU÷F‚çw&—FU÷FW‡B‚'·Ò"ÂVæ6öF–æsÒ'WFbÓ‚"Ğ¢&÷BÒf¶T&÷B‚Ğ¢G&gBÒÆ–6F–öäG&gB‡f6æ7’Â&ÖÅöVæv–æVW&–ær"Â&W7VÖRÂ""Ğ¢v—F‚€Ğ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æfWF6…ö¦ö%ög&öÕöÖW76vR"ÂæWsÔ7–æ4Öö6²‡&WGW&å÷fÇVS×vR’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æ—5ö†—&–g•ö¦ö%÷W&Â"Â&WGW&å÷fÇVSÔfÇ6R’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æ'V–ÆEöÆ–6F–öåöf÷%÷f6æ7’"Â&WGW&å÷fÇVSÖG&gB’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$¤ô%5ôD%õD‚"ÂF%÷F‚’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ%$U5TÔUôD•""Â&ö÷B’ÀĞ¢“ Ğ¢v—Bö†æFÆU÷Fö¶Våög&VR…6–×ÆTæÖW76R†&÷CÖ&÷B’ÂW&ÂĞ Ğ¢&Wf–WrÒ&÷BæÖW76vW5²ÓĞĞ¢'WGFöåöFFÒ&Wf–Wu²'&WÇ•öÖ&·W%Òæ–æÆ–æUö¶W–&ö&E³Õ³Òæ6ÆÆ&6µöFFĞ¢6VÆbæ76W'EG'VR†'WGFöåöFFç7F'G7v—F‚‚'vV&Ç“¢"’Ğ¢VW'’Òf¶UVW'’†'WGFöåöFFĞ¢7V&Ö—BÒ7–æ4Öö6²‡&WGW&å÷fÇVSÒ&‡GG3¢ò÷wwræ¦ö'÷7F–ærç&òöÆ–6F–öâ÷7V66W72"Ğ¢v—F‚€Ğ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2ç7V&Ö—EöÆ–6F–öâ"ÂæWs×7V&Ö—B’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$¤ô%5ôD%õD‚"ÂF%÷F‚’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ%$U5TÔUôD•""Â&ö÷B’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$Ä”4D”ôåõ$ôd”ÄUõD‚"Â&öf–ÆU÷F‚’ÀĞ¢“ Ğ¢v—B†æFÆUö6ÆÆ&6²…6–×ÆTæÖW76R†6ÆÆ&6µ÷VW'“×VW'’’Â6–×ÆTæÖW76R‚’Ğ¢7V&Ö—Bæ76W'Eöv—FVEööæ6U÷v—F‚‡W&ÂÂ&W7VÖRÂ&öf–ÆU÷F‚Â""Ğ¢6VÆbæ76W'D–â‚$Æ–6F–öâ7V&Ö—GFVB"ÂVW'’æVF—FVBĞ Ğ¢7–æ2FVbFW7Eö6†'•öGWÆ–6FUö6ÆÆ&6µ÷7V&Ö—G5ööæ6R‡6VÆb“ Ğ¢W&ÂÒ&‡GG3¢òö¦ö'2æ6†'–‡æ6öÒö6Æ—&ö&BöCsv####BÓ3vbÓC†#ÖVÖ#csS3““63 Ğ¢f6æ7’Òf6æ7’€Ğ¢F—FÆSÒ%FV6†æ–6Â7W÷'BVæv–æVW""ÀĞ¢6ö×ç“Ò$6Æ—&ö&B"ÀĞ¢FW67&—F–öãÒ%—F†öâ7W÷'BVæv–æVW&–ærG&÷V&ÆW6†ö÷F–ær&öÆR"¢RÀĞ¢W&Ã×W&ÂÀĞ¢Ğ¢vRÒ'6VD¦ö%vR€Ğ¢f6æ7’Â&6†'•öÆ–6F–öåöf÷&Ò"ÂW&Â²"öÆ–6F–öâ"ÂW&ÂÀĞ¢6öçF7Eö¶–æCÒ&G2"Â6öçF7E÷fÇVSÒ&6†'’"ÀĞ¢Ğ¢&VfÆ–v‡BÒG5&VfÆ–v‡B‚&6†'’"ÂvRÂ‚’Âö&¦V7B‚’Ğ¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2F—&V7F÷'“ Ğ¢&ö÷BÒF‚†F—&V7F÷'’Ğ¢&W7VÖRÒ&ö÷Bò&&6¶VæBçFb Ğ¢&W7VÖRçw&—FUö'—FW2†"'Fb"Ğ¢&öf–ÆU÷F‚Ò&ö÷Bò&Æ–6çBæ§6öâ Ğ¢&öf–ÆU÷F‚çw&—FU÷FW‡B‚'·Ò"ÂVæ6öF–æsÒ'WFbÓ‚"Ğ¢F%÷F‚Ò&ö÷Bò&¦ö'2æF" Ğ¢&÷BÒf¶T&÷B‚Ğ¢G&gBÒÆ–6F–öäG&gB‡f6æ7’Â&&6¶VæE÷—F†öâ"Â&W7VÖRÂ""Ğ¢v—F‚€Ğ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æfWF6…öG5÷vR"ÂæWsÔ7–æ4Öö6²‡&WGW&å÷fÇVS×vR’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2ç&VfÆ–v‡EöG5öÆ–6F–öâ"ÂæWsÔ7–æ4Öö6²‡&WGW&å÷fÇVS×&VfÆ–v‡B’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æ'V–ÆEöÆ–6F–öåöf÷%÷f6æ7’"Â&WGW&å÷fÇVSÖG&gB’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$¤ô%5ôD%õD‚"ÂF%÷F‚’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ%$U5TÔUôD•""Â&ö÷B’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$Ä”4D”ôåõ$ôd”ÄUõD‚"Â&öf–ÆU÷F‚’ÀĞ¢“ Ğ¢v—Bö†æFÆU÷Fö¶Våög&VR…6–×ÆTæÖW76R†&÷CÖ&÷B’ÂW&ÂĞ Ğ¢'WGFöåöFFÒ&÷BæÖW76vW5²ÓÕ²'&WÇ•öÖ&·W%Òæ–æÆ–æUö¶W–&ö&E³Õ³Òæ6ÆÆ&6µöFFĞ¢6VÆbæ76W'EG'VR†'WGFöåöFFç7F'G7v—F‚‚&G6Ç“¢"’Ğ¢7V&Ö—BÒ7–æ4Öö6²‡&WGW&å÷fÇVSÔG57V&Ö—76–öå&W7VÇB€Ğ¢'7V&Ö—GFVB"ÂW&Â²"öÆ–6F–öâ÷7V66W72"Â&6öæf—&ÖVB Ğ¢’Ğ¢v—F‚€Ğ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2ç7V&Ö—EöG5öÆ–6F–öâ"ÂæWs×7V&Ö—B’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$¤ô%5ôD%õD‚"ÂF%÷F‚’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ%$U5TÔUôD•""Â&ö÷B’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$Ä”4D”ôåõ$ôd”ÄUõD‚"Â&öf–ÆU÷F‚’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$4„%•ô%$õu4U%õ$ôd”ÄUõD‚"Â&ö÷Bò&'&÷w6W""’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$E5ô%$õu4U%ô„TDÄU52"ÂG'VR’À¢“ Ğ¢f—'7BÒf¶UVW'’†'WGFöåöFFĞ¢v—B†æFÆUö6ÆÆ&6²…6–×ÆTæÖW76R†6ÆÆ&6µ÷VW'“Öf—'7B’Â6–×ÆTæÖW76R‚’Ğ¢GWÆ–6FRÒf¶UVW'’†'WGFöåöFFĞ¢v—B†æFÆUö6ÆÆ&6²…6–×ÆTæÖW76R†6ÆÆ&6µ÷VW'“ÖGWÆ–6FR’Â6–×ÆTæÖW76R‚’Ğ Ğ¢7V&Ö—Bæ76W'Eöv—FVEööæ6R‚Ğ¢6VÆbæ76W'D–â‚$Æ–6F–öâ7V&Ö—GFVB"Âf—'7BæVF—FVBĞ¢6VÆbæ76W'D–â‚&Ç&VG’6VæF–ær÷"6VçB"ÂGWÆ–6FRæVF—FVBĞ Ğ¢7–æ2FVbFW7E÷FVÆVw&Õö6öçF7E÷&Wf–WuöæEööæUö'WGFöå÷6VæEöG'•÷'Vâ‡6VÆb“ Ğ¢W&ÂÒ&‡GG3¢òö†—&–g’æÖRö¦ö'2ós3#rÖÆ–6F–öâÖ&6¶VæBÖVæv–æVW"×—F†öâ Ğ¢f6æ7’Òf6æ7’€Ğ¢F—FÆSÒ$Æ–6F–öâ&6¶VæBVæv–æVW"…—F†öâ’"ÀĞ¢6ö×ç“Ò#32"ÀĞ¢FW67&—F–öãÒ%—F†öâf7D’&6¶VæBVæv–æVW&–ær&öÆR"¢RÀĞ¢W&Ã×W&ÂÀĞ¢Ğ¢vRÒ'6VD¦ö%vR‡f6æ7’Â'7G'V7GW&VEö¦ö%÷vR"Â""ÂW&ÂĞ Ğ¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’‚’2F—&V7F÷'“ Ğ¢&ö÷BÒF‚†F—&V7F÷'’Ğ¢&W7VÖRÒ&ö÷Bò&&6¶VæBçFb Ğ¢&W7VÖRçw&—FUö'—FW2†"'Fb"Ğ¢F%÷F‚Ò&ö÷Bò&¦ö'2æF" Ğ¢&÷BÒf¶T&÷B‚Ğ¢7G‚Ò6–×ÆTæÖW76R†&÷CÖ&÷BĞ¢G&gBÒÆ–6F–öäG&gB‡f6æ7’Â&&6¶VæE÷—F†öâ"Â&W7VÖRÂ&öÆBvVæW&–2ÖW76vR"Ğ Ğ¢v—F‚€Ğ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æfWF6…ö¦ö%ög&öÕöÖW76vR"ÂæWsÔ7–æ4Öö6²‡&WGW&å÷fÇVS×vR’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2åövWEö†—&–g•ö6Æ–VçB"Â&WGW&å÷fÇVSÔf¶T†—&–g”6Æ–VçB‚’’ÀĞ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2æ'V–ÆEöÆ–6F–öåöf÷%÷f6æ7’"Â&WGW&å÷fÇVSÖG&gB’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$¤ô%5ôD%õD‚"ÂF%÷F‚’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ%$U5TÔUôD•""Â&ö÷B’ÀĞ¢“ Ğ¢v—Bö†æFÆU÷Fö¶Våög&VR†7G‚ÂW&ÂĞ Ğ¢7VÖÖ'’Ò&÷BæÖW76vW5³Õ²'FW‡B%ĞĞ¢6VÆbæ76W'Dæ÷D–â‚$¦ö"”B"Â7VÖÖ'’Ğ¢6VÆbæ76W'Dæ÷D–â‚%6÷W&6S¢"Â7VÖÖ'’Ğ¢6VÆbæ76W'D–â‚$6öçF7C¢'FVÕög6–Wf–6‚"Â7VÖÖ'’Ğ¢6VÆbæ76W'DWVÂ†ÆVâ†&÷BæFö7VÖVçG2’ÂĞ Ğ¢&Wf–WrÒ&÷BæÖW76vW5³%ĞĞ¢6VÆbæ76W'D–â‚-	ı-]---=âÂ]í}2í-­½­İ=-Íò"Â&Wf–Wu²'FW‡B%ÒĞ¢6VÆbæ76W'D–â‡W&ÂÂ&Wf–Wu²'FW‡B%ÒĞ¢6VÆbæ76W'Dæ÷D–â†br'·W&ÇÒ"rÂ&Wf–Wu²'FW‡B%ÒĞ¢'WGFöåöFFÒ&Wf–Wu²'&WÇ•öÖ&·W%Òæ–æÆ–æUö¶W–&ö&E³Õ³Òæ6ÆÆ&6µöFFĞ¢6VÆbæ76W'EG'VR†'WGFöåöFFç7F'G7v—F‚‚&Ç“¢"’Ğ Ğ¢VW'’Òf¶UVW'’†'WGFöåöFFĞ¢WFFRÒ6–×ÆTæÖW76R†6ÆÆ&6µ÷VW'“×VW'’Ğ¢f¶U6VæFW"æ6ÆÇ2æ6ÆV"‚Ğ¢v—F‚€Ğ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2åFVÆVw&Õ6VæFW""Âf¶U6VæFW"’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$¤ô%5ôD%õD‚"ÂF%÷F‚’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ%$U5TÔUôD•""Â&ö÷B’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ%DTÄTu$Õô•ô”B"Â’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ%DTÄTu$Õô•ô„4‚"Â&†6‚"’ÀĞ¢“ Ğ¢v—B†æFÆUö6ÆÆ&6²‡WFFRÂ6–×ÆTæÖW76R‚’Ğ Ğ¢6VÆbæ76W'DWVÂ„f¶U6VæFW"æ6ÆÇ5³Õ³ÒÂ&'FVÕög6–Wf–6‚"Ğ¢6VÆbæ76W'DWVÂ„f¶U6VæFW"æ6ÆÇ5³Õ³%ÒÂ&&6¶VæBçFb"Ğ¢6VÆbæ76W'D–â‚-	ı-]---=âÂ]í}2í-­½­İ=-Íò"Âf¶U6VæFW"æ6ÆÇ5³Õ³ÒĞ¢6VÆbæ76W'D–â‚%6VçBFò'FVÕög6–Wf–6‚"ÂVW'’æVF—FVBĞ Ğ¢GWÆ–6FU÷VW'’Òf¶UVW'’†'WGFöåöFFĞ¢v—F‚€Ğ¢F6‚‚&¦ö&&÷Bæ†æFÆW'2åFVÆVw&Õ6VæFW""Âf¶U6VæFW"’ÀĞ¢F6‚æö&¦V7B†6öæf–rÂ$¤ô%5ôD%õD‚"ÂF%÷F‚’ÀĞ¢“ Ğ¢v—B†æFÆUö6ÆÆ&6²…6–×ÆTæÖW76R†6ÆÆ&6µ÷VW'“ÖGWÆ–6FU÷VW'’’Â6–×ÆTæÖW76R‚’Ğ¢6VÆbæ76W'DWVÂ†ÆVâ„f¶U6VæFW"æ6ÆÇ2’ÂĞ¢6VÆbæ76W'D–â‚&Ç&VG’6VæF–ær÷"6VçB"ÂGWÆ–6FU÷VW'’æVF—FVBĞ Ğ Ğ¦–bõöæÖUõòÓÒ%õöÖ–åõò# Ğ¢Væ—GFW7BæÖ–â‚Ğ 