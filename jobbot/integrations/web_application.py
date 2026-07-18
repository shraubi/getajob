"""Generic one-click application submission for conventional HTML forms."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from jobbot.integrations.job_page import validate_public_url
from jobbot.application import extract_resume_text

_MAX_PAGE_BYTES = 2_000_000
_SUCCESS_RE = re.compile(
    r"thank\s+you|application\s+(?:was\s+)?(?:sent|submitted|received)|"
    r"candidature\s+(?:a\s+ete\s+)?(?:envoyee|transmise)|merci\s+pour\s+votre\s+candidature",
    re.I,
)


class WebApplicationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApplicantProfile:
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    answers: dict[str, str] | None = None


def load_profile(path: Path, resume_path: Path) -> ApplicantProfile:
    raw = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WebApplicationError(f"Applicant profile is invalid: {exc}") from exc
    try:
        resume_text = extract_resume_text(resume_path)
    except Exception:
        resume_text = ""
    lines = [line.strip() for line in resume_text.splitlines() if line.strip()]
    name_parts = lines[0].split() if lines else []
    email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", resume_text)
    phone_match = re.search(r"\+?\d[\d\s().-]{7,}\d", resume_text)
    return ApplicantProfile(
        first_name=str(raw.get("first_name") or (name_parts[0] if name_parts else "")).strip(),
        last_name=str(raw.get("last_name") or (" ".join(name_parts[1:]) if len(name_parts) > 1 else "")).strip(),
        email=str(raw.get("email") or (email_match.group(0) if email_match else "")).strip(),
        phone=str(raw.get("phone") or (phone_match.group(0) if phone_match else "")).strip(),
        answers={str(key): str(value) for key, value in dict(raw.get("answers") or {}).items()},
    )


def _field_key(tag) -> str:
    return " ".join(str(tag.get(key, "")) for key in ("name", "id", "placeholder", "aria-label")).casefold()


def _profile_value(tag, profile: ApplicantProfile) -> str:
    key = _field_key(tag)
    if any(marker in key for marker in ("first_name", "firstname", "first name", "prenom", "given")):
        return profile.first_name
    if any(marker in key for marker in ("last_name", "lastname", "last name", "surname", "nom", "family")):
        return profile.last_name
    if "mail" in key or tag.get("type") == "email":
        return profile.email
    if any(marker in key for marker in ("phone", "tel", "mobile")) or tag.get("type") == "tel":
        return profile.phone
    return ""


def _searchable_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def build_form_payload(html: str, page_url: str, profile: ApplicantProfile, message: str = ""):
    soup = BeautifulSoup(html, "html.parser")
    form = next((item for item in soup.find_all("form") if item.find("input", attrs={"type": "file"})), None)
    if form is None:
        raise WebApplicationError("No resume application form was found")
    method = str(form.get("method", "get")).casefold()
    if method != "post":
        raise WebApplicationError(f"Unsupported application form method: {method}")
    action = urljoin(page_url, form.get("action") or page_url)
    data: dict[str, str] = {}
    missing: list[str] = []
    file_field = ""
    radio_groups: dict[str, list] = {}

    for tag in form.find_all(["input", "textarea", "select"]):
        name = str(tag.get("name", "")).strip()
        if not name or tag.has_attr("disabled"):
            continue
        kind = str(tag.get("type", tag.name)).casefold()
        if kind in {"submit", "button", "reset", "image"}:
            continue
        if kind == "file":
            file_field = name
            continue
        if kind == "radio":
            radio_groups.setdefault(name, []).append(tag)
            continue
        if kind == "checkbox":
            answer = (profile.answers or {}).get(name)
            if answer is not None:
                if answer.casefold() in {"1", "true", "yes", "on"}:
                    data[name] = str(tag.get("value", "on"))
                elif tag.has_attr("required"):
                    missing.append(name)
            elif tag.has_attr("required"):
                missing.append(name)
            continue
        if kind == "hidden":
            data[name] = str(tag.get("value", ""))
            continue
        value = (profile.answers or {}).get(name, "")
        if not value and tag.name == "textarea" and any(word in _field_key(tag) for word in ("motiv", "message", "cover")):
            value = message
        if not value:
            value = _profile_value(tag, profile)
        if value:
            data[name] = value
        elif tag.has_attr("required"):
            missing.append(name)

    for name, options in radio_groups.items():
        answer = (profile.answers or {}).get(name)
        values = {str(option.get("value", "on")) for option in options}
        if answer in values:
            data[name] = answer
        elif any(option.has_attr("required") for option in options):
            missing.append(name)

    if not file_field:
        raise WebApplicationError("The application form has no resume upload field")
    if missing:
        raise WebApplicationError("Applicant profile is missing required fields: " + ", ".join(sorted(set(missing))))
    return action, data, file_field


async def _read_response(response: httpx.Response) -> str:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAX_PAGE_BYTES:
            raise WebApplicationError("Application response exceeded the safe size limit")
    return bytes(body).decode(response.encoding or "utf-8", errors="replace")


async def submit_application(
    page_url: str,
    resume_path: Path,
    profile_path: Path,
    message: str = "",
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    host = (urlparse(page_url).hostname or "").casefold()
    if host == "getmatch.ru" or host.endswith(".getmatch.ru"):
        raise WebApplicationError("Getmatch requires a signed-in candidate session and email OTP before applying")
    if host == "koronatech.ru" or host.endswith(".koronatech.ru"):
        raise WebApplicationError("KoronaTech requires its visual CAPTCHA before the resume can be submitted")
    if not resume_path.is_file():
        raise WebApplicationError(f"Resume is missing: {resume_path.name}")
    if resume_path.stat().st_size > 2_000_000:
        raise WebApplicationError("Resume exceeds the 2 MB application limit")
    profile = load_profile(profile_path, resume_path)
    headers = {"User-Agent": "getajob/1.0", "Accept": "text/html,application/xhtml+xml"}
    async def validate_request(request: httpx.Request) -> None:
        await validate_public_url(str(request.url))

    async with httpx.AsyncClient(
        headers=headers, timeout=20.0, follow_redirects=True, transport=transport,
        event_hooks={"request": [validate_request]},
    ) as client:
        await validate_public_url(page_url)
        async with client.stream("GET", page_url) as response:
            response.raise_for_status()
            html = await _read_response(response)
        action, data, file_field = build_form_payload(html, page_url, profile, message)
        await validate_public_url(action)
        with resume_path.open("rb") as resume:
            async with client.stream(
                "POST", action, data=data,
                files={file_field: (resume_path.name, resume, "application/pdf")},
            ) as response:
                response.raise_for_status()
                result_html = await _read_response(response)
        result_soup = BeautifulSoup(result_html, "html.parser")
        result_text = _searchable_text(result_soup.get_text(" ", strip=True))
        confirmed = bool(_SUCCESS_RE.search(result_text))
        redirected_to_confirmation = str(response.url) != action and not result_soup.find("input", attrs={"type": "file"})
        if not confirmed and not redirected_to_confirmation:
            raise WebApplicationError("The application form was returned without a success confirmation")
        return str(response.url)
