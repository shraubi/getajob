"""Pure token-free parsing, resume selection, and application drafting."""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from jobbot.classifier import classify, score_directions

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>]+")
_SALARY_RE = re.compile(r"(?:\d[\d\s.,]*[-–]\s*)?\d[\d\s.,]*\s*(?:EUR|USD|GBP|\u20ac|\$|\u00a3|\u20bd|RUB)", re.I)
_SKILLS_PREFIXES = ("skills:", "\u043d\u0430\u0432\u044b\u043a\u0438:")
_SUBSCRIPTION_PREFIX = "\u043f\u043e \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0435:"
_REMOTE_WORDS = {"remote", "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u043e", "\u0443\u0434\u0430\u043b\u0451\u043d\u043d\u043e"}
_EMPLOYMENT_WORDS = {"full-time", "full time", "part-time", "part time", "\u0444\u0443\u043b\u043b-\u0442\u0430\u0439\u043c", "\u043f\u0430\u0440\u0442-\u0442\u0430\u0439\u043c"}
_SENIORITY_WORDS = {"intern", "junior", "middle", "senior", "lead", "\u0441\u0442\u0430\u0436\u0435\u0440"}
_JOB_BOARD_DOMAINS = {"hellowork.com", "linkedin.com", "indeed.com", "hh.ru", "welcometothejungle.com"}
_ATS_DOMAINS = {"greenhouse.io", "lever.co", "myworkdayjobs.com", "ashbyhq.com", "smartrecruiters.com"}


@dataclass(frozen=True)
class Vacancy:
    title: str
    company: str
    description: str
    url: str = ""
    source_category: str = "unknown"
    salary: str = ""
    work_format: str = ""
    employment: str = ""
    seniority: str = ""
    location: str = ""
    language: str = ""
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApplicationDraft:
    vacancy: Vacancy
    direction: str
    resume_path: Path
    message: str


class UnknownDirectionError(ValueError):
    pass


class ResumeNotFoundError(FileNotFoundError):
    pass


def categorize_source(text: str, url: str = "") -> str:
    if not url:
        folded = text.casefold()
        if any(prefix in folded for prefix in _SKILLS_PREFIXES) or _SUBSCRIPTION_PREFIX in folded:
            return "telegram_lead"
        return "telegram_message"
    host = (urlparse(url).hostname or "").casefold()
    if any(host == domain or host.endswith("." + domain) for domain in _ATS_DOMAINS):
        return "ats"
    if any(host == domain or host.endswith("." + domain) for domain in _JOB_BOARD_DOMAINS):
        return "job_board"
    if any(part in urlparse(url).path.casefold() for part in ("/career", "/jobs", "/vacanc")):
        return "company_careers"
    return "web_page"


def _tag_value(parts: list[str], candidates: set[str]) -> str:
    for part in parts:
        if part.casefold() in candidates:
            return part
    return ""


def parse_vacancy(text: str) -> Vacancy:
    clean = text.strip()
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    url_match = _URL_RE.search(clean)
    url = url_match.group(0).rstrip(".,);]") if url_match else ""
    title = lines[0][:160] if lines else "Vacancy"
    company = "Unknown company"
    salary = ""
    language = ""
    skills: tuple[str, ...] = ()
    metadata: list[str] = []

    for line in lines:
        folded = line.casefold()
        key, separator, value = line.partition(":")
        if separator and key.casefold() in {"company", "\u043a\u043e\u043c\u043f\u0430\u043d\u0438\u044f"} and value.strip():
            company = value.strip()[:120]
        elif separator and key.casefold() in {"title", "role", "position", "\u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f"} and value.strip():
            title = value.strip()[:160]
        elif any(folded.startswith(prefix) for prefix in _SKILLS_PREFIXES):
            raw_skills = line.split(":", 1)[1]
            skills = tuple(item.strip() for item in raw_skills.split(",") if item.strip())
        elif not salary and _SALARY_RE.search(line):
            salary = line
        elif "english" in folded or "\u0430\u043d\u0433\u043b\u0438\u0439\u0441\u043a" in folded:
            language = line.lstrip("\U0001f1ec\U0001f1e7 ")
        elif "|" in line:
            metadata.extend(item.strip() for item in line.split("|") if item.strip())

    work_format = _tag_value(metadata, _REMOTE_WORDS)
    employment = _tag_value(metadata, _EMPLOYMENT_WORDS)
    seniority = _tag_value(metadata, _SENIORITY_WORDS)
    known = {work_format, employment, seniority, ""}
    location = next((item for item in metadata if item not in known and not re.fullmatch(r"[A-C][12]", item, re.I)), "")
    return Vacancy(
        title=title,
        company=company,
        description=clean,
        url=url,
        source_category=categorize_source(clean, url),
        salary=salary,
        work_format=work_format,
        employment=employment,
        seniority=seniority,
        location=location,
        language=language,
        skills=skills,
    )


def extract_resume_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages[:5])


def classify_resume(path: Path) -> str:
    extraction_error = ""
    try:
        text = extract_resume_text(path)
    except Exception as exc:
        text = ""
        extraction_error = f"{type(exc).__name__}: {exc}"
    text_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if text_lines:
        role_hint = text_lines[1] if len(text_lines) > 1 else text_lines[0]
        classification_text = text
        source = "pdf_text"
    else:
        role_hint = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", path.stem)
        role_hint = role_hint.replace("_", " ").replace("-", " ")
        classification_text = ""
        source = "filename"
    scores = score_directions(role_hint, classification_text)
    direction = classify(role_hint, classification_text)
    logger.info(
        "Resume classification file=%s source=%s role_hint=%r extracted_chars=%d direction=%s scores=%s extraction_error=%s",
        path.name, source, role_hint, len(text), direction, scores, extraction_error or "none",
    )
    return direction


def discover_resumes(resume_dir: Path) -> dict[str, Path]:
    if not resume_dir.is_dir():
        raise ResumeNotFoundError(f"Resume directory is missing: {resume_dir}")
    result: dict[str, Path] = {}
    for path in sorted(resume_dir.glob("*.pdf")):
        direction = classify_resume(path)
        if direction != "other":
            if direction in result:
                logger.warning("Multiple resumes classified as %s; keeping %s and ignoring %s", direction, result[direction].name, path.name)
            result.setdefault(direction, path)
    logger.info("Resume inventory directory=%s selected=%s", resume_dir, {key: value.name for key, value in result.items()})
    return result


def select_resume(direction: str, resume_dir: Path) -> Path:
    path = discover_resumes(resume_dir).get(direction)
    if path is None:
        raise ResumeNotFoundError(f"No PDF resume in {resume_dir} was classified as {direction}")
    return path


def render_message(vacancy: Vacancy, direction: str) -> str:
    # A deterministic template cannot make truthful, role-specific claims from
    # the vacancy alone. Prefer sending the selected resume without filler.
    return ""


def render_telegram_message(vacancy_url: str) -> str:
    return (
        "Приветствую, хочу откликнуться вот на эту вакансию:\n"
        f"{vacancy_url}\n"
        "Резюме прикрепляю. Буду рада пообщаться подробнее"
    )


def format_vacancy_summary(vacancy: Vacancy, direction: str, job_id: str = "") -> str:
    rows = [f"Source: {vacancy.source_category}", f"Direction: {direction}", f"Role: {vacancy.title}"]
    if vacancy.company != "Unknown company":
        rows.append(f"Company: {vacancy.company}")
    for label, value in (("Salary", vacancy.salary), ("Location", vacancy.location), ("Format", vacancy.work_format), ("Employment", vacancy.employment), ("Seniority", vacancy.seniority), ("Language", vacancy.language)):
        if value:
            rows.append(f"{label}: {value}")
    if vacancy.skills:
        rows.append("Skills: " + ", ".join(vacancy.skills))
    if job_id:
        rows.append(f"Saved job: {job_id[:12]}")
    return "\n".join(rows)


def build_application_for_vacancy(vacancy: Vacancy, resume_dir: Path) -> ApplicationDraft:
    direction = classify(vacancy.title, vacancy.description)
    if direction == "other":
        raise UnknownDirectionError("Could not confidently classify this vacancy")
    resume_path = select_resume(direction, resume_dir)
    return ApplicationDraft(vacancy, direction, resume_path, render_message(vacancy, direction))


def build_application(text: str, resume_dir: Path) -> ApplicationDraft:
    return build_application_for_vacancy(parse_vacancy(text), resume_dir)
