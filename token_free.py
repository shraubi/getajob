"""Pure token-free application flow used by the Telegram shell."""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from classifier import classify, score_directions

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>]+")
_DIRECTION_LABELS = {
    "backend_python": "Python backend",
    "data_engineering": "data engineering",
    "ml_engineering": "ML engineering",
    "devops": "DevOps",
    "tech_support": "technical support",
}


@dataclass(frozen=True)
class Vacancy:
    title: str
    company: str
    description: str
    url: str = ""


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


def parse_vacancy(text: str) -> Vacancy:
    clean = text.strip()
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    url_match = _URL_RE.search(clean)
    title = lines[0][:120] if lines else "Vacancy"
    company = "Unknown company"
    if len(lines) > 1 and len(lines[1]) <= 100 and not _URL_RE.fullmatch(lines[1]):
        company = lines[1]
    for line in lines[:5]:
        key, separator, value = line.partition(":")
        if separator and key.casefold() == "company" and value.strip():
            company = value.strip()[:100]
        if separator and key.casefold() in {"title", "role", "position"} and value.strip():
            title = value.strip()[:120]
    return Vacancy(
        title=title,
        company=company,
        description=clean,
        url=url_match.group(0).rstrip(".,);]") if url_match else "",
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
        path.name,
        source,
        role_hint,
        len(text),
        direction,
        scores,
        extraction_error or "none",
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
                logger.warning(
                    "Multiple resumes classified as %s; keeping %s and ignoring %s",
                    direction,
                    result[direction].name,
                    path.name,
                )
            result.setdefault(direction, path)
    logger.info(
        "Resume inventory directory=%s selected=%s",
        resume_dir,
        {direction: path.name for direction, path in result.items()},
    )
    return result


def select_resume(direction: str, resume_dir: Path) -> Path:
    path = discover_resumes(resume_dir).get(direction)
    if path is None:
        raise ResumeNotFoundError(
            f"No PDF resume in {resume_dir} was classified as {direction}"
        )
    return path


def render_message(vacancy: Vacancy, direction: str) -> str:
    label = _DIRECTION_LABELS.get(direction, direction)
    url_line = f"\n\nVacancy: {vacancy.url}" if vacancy.url else ""
    return (
        f"Hi! I'm applying for the {vacancy.title} role at {vacancy.company}. "
        f"My {label} background aligns with the role, and I've attached the most relevant "
        "version of my resume. I'd be glad to discuss the team's priorities."
        f"{url_line}"
    )


def build_application_for_vacancy(vacancy: Vacancy, resume_dir: Path) -> ApplicationDraft:
    direction = classify(vacancy.title, vacancy.description)
    if direction == "other":
        raise UnknownDirectionError("Could not confidently classify this vacancy")
    resume_path = select_resume(direction, resume_dir)
    return ApplicationDraft(
        vacancy=vacancy,
        direction=direction,
        resume_path=resume_path,
        message=render_message(vacancy, direction),
    )


def build_application(text: str, resume_dir: Path) -> ApplicationDraft:
    return build_application_for_vacancy(parse_vacancy(text), resume_dir)
