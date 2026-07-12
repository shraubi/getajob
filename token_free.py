"""Pure token-free application flow used by the Telegram shell."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from classifier import classify


_URL_RE = re.compile(r"https?://[^\s<>]+")
_CYRILLIC_RE = re.compile(r"[Ð-Ð¯Ð°-ÑÐÑ‘]")
_DIRECTION_LABELS = {
    "backend_python": ("Python backend", "Python backend"),
    "data_engineering": ("data engineering", "data engineering"),
    "ml_engineering": ("ML engineering", "ML engineering"),
    "devops": ("DevOps", "DevOps"),
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
    """Extract useful metadata while retaining the full text for classification."""
    clean = text.strip()
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    url_match = _URL_RE.search(clean)

    title = lines[0][:120] if lines else "Vacancy"
    company = "Unknown company"
    if len(lines) > 1 and len(lines[1]) <= 100 and not _URL_RE.fullmatch(lines[1]):
        company = lines[1]

    for line in lines[:5]:
        key, separator, value = line.partition(":")
        if separator and key.casefold() in {"company", "ÐºÐ¾Ð¼Ð¿Ð°Ð½Ð¸Ñ"} and value.strip():
            company = value.strip()[:100]
        if separator and key.casefold() in {"title", "role", "position", "Ð²Ð°ÐºÐ°Ð½ÑÐ¸Ñ"} and value.strip():
            title = value.strip()[:120]

    return Vacancy(
        title=title,
        company=company,
        description=clean,
        url=url_match.group(0).rstrip(".,);]") if url_match else "",
    )


def select_resume(
    direction: str,
    resume_dir: Path,
    resume_files: Mapping[str, str],
) -> Path:
    filename = resume_files.get(direction)
    if not filename:
        raise UnknownDirectionError(f"No resume is configured for direction: {direction}")
    path = resume_dir / filename
    if not path.is_file():
        raise ResumeNotFoundError(f"Resume file is missing: {path}")
    return path


def render_message(vacancy: Vacancy, direction: str) -> str:
    label_en, label_ru = _DIRECTION_LABELS.get(direction, (direction, direction))
    url_line = f"\n\nVacancy: {vacancy.url}" if vacancy.url else ""
    if _CYRILLIC_RE.search(vacancy.description):
        url_line = f"\n\nÐ’Ð°ÐºÐ°Ð½ÑÐ¸Ñ: {vacancy.url}" if vacancy.url else ""
        return (
            f"Ð—Ð´Ñ€Ð°Ð²ÑÑ‚Ð²ÑƒÐ¹Ñ‚Ðµ! ÐžÑ‚ÐºÐ»Ð¸ÐºÐ°ÑŽÑÑŒ Ð½Ð° Ð¿Ð¾Ð·Ð¸Ñ†Ð¸ÑŽ Â«{vacancy.title}Â» Ð² {vacancy.company}. "
            f"ÐœÐ¾Ð¹ Ð¾Ð¿Ñ‹Ñ‚ Ð² Ð½Ð°Ð¿Ñ€Ð°Ð²Ð»ÐµÐ½Ð¸Ð¸ {label_ru} ÑÐ¾Ð¾Ñ‚Ð²ÐµÑ‚ÑÑ‚Ð²ÑƒÐµÑ‚ Ð¿Ñ€Ð¾Ñ„Ð¸Ð»ÑŽ Ñ€Ð¾Ð»Ð¸. "
            "ÐŸÑ€Ð¸ÐºÐ»Ð°Ð´Ñ‹Ð²Ð°ÑŽ Ð½Ð°Ð¸Ð±Ð¾Ð»ÐµÐµ Ñ€ÐµÐ»ÐµÐ²Ð°Ð½Ñ‚Ð½ÑƒÑŽ Ð²ÐµÑ€ÑÐ¸ÑŽ Ñ€ÐµÐ·ÑŽÐ¼Ðµ Ð¸ Ð±ÑƒÐ´Ñƒ Ñ€Ð°Ð´ Ð¾Ð±ÑÑƒÐ´Ð¸Ñ‚ÑŒ Ð·Ð°Ð´Ð°Ñ‡Ð¸ ÐºÐ¾Ð¼Ð°Ð½Ð´Ñ‹."
            f"{url_line}"
        )
    return (
        f"Hi! I'm applying for the {vacancy.title} role at {vacancy.company}. "
        f"My {label_en} background aligns with the role, and I've attached the most relevant "
        "version of my rÃ©sumÃ©. I'd be glad to discuss the team's priorities."
        f"{url_line}"
    )


def build_application(
    text: str,
    resume_dir: Path,
    resume_files: Mapping[str, str],
) -> ApplicationDraft:
    vacancy = parse_vacancy(text)
    direction = classify(vacancy.title, vacancy.description)
    if direction == "other":
        raise UnknownDirectionError("Could not confidently classify this vacancy")
    resume_path = select_resume(direction, resume_dir, resume_files)
    return ApplicationDraft(
        vacancy=vacancy,
        direction=direction,
        resume_path=resume_path,
        message=render_message(vacancy, direction),
    )
