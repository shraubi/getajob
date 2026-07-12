"""Deterministic multilingual, role-first vacancy classification."""

from collections.abc import Mapping
import re

DEFAULT_WEIGHTS: dict[str, dict[str, int]] = {
    "backend_python": {
        "python": 6, "fastapi": 6, "django": 6, "flask": 5,
        "sqlalchemy": 3, "asyncio": 2, "backend": 2, "Ð±ÐµÐºÐµÐ½Ð´": 2, "Ð±ÑÐºÐµÐ½Ð´": 2,
    },
    "data_engineering": {
        "data engineer": 7, "Ð¸Ð½Ð¶ÐµÐ½ÐµÑ€ Ð´Ð°Ð½Ð½Ñ‹Ñ…": 7, "Ð´Ð°Ñ‚Ð° Ð¸Ð½Ð¶ÐµÐ½ÐµÑ€": 7,
        "databricks": 5, "airflow": 4, "spark": 4, "etl": 3,
        "data pipeline": 3, "Ð¿Ð°Ð¹Ð¿Ð»Ð°Ð¹Ð½ Ð´Ð°Ð½Ð½Ñ‹Ñ…": 3, "dbt": 3, "warehouse": 2,
    },
    "ml_engineering": {
        "machine learning": 7, "ml engineer": 7, "ai engineer": 7,
        "Ð¸Ð½Ð¶ÐµÐ½ÐµÑ€ Ð¸Ð¸": 7, "Ñ€Ð°Ð·Ñ€Ð°Ð±Ð¾Ñ‚Ñ‡Ð¸Ðº Ð¸Ð¸": 7, "Ð¸Ð¸ Ð°Ð³ÐµÐ½Ñ‚": 6,
        "Ð¸ÑÐºÑƒÑÑÑ‚Ð²ÐµÐ½Ð½Ñ‹Ð¹ Ð¸Ð½Ñ‚ÐµÐ»Ð»ÐµÐºÑ‚": 6, "llm": 5, "ai agents": 5,
        "pytorch": 4, "tensorflow": 4, "mlops": 3,
    },
    "devops": {
        "devops": 8, "sre": 7, "platform engineer": 7,
        "Ð¸Ð½Ð¶ÐµÐ½ÐµÑ€ Ð¸Ð½Ñ„Ñ€Ð°ÑÑ‚Ñ€ÑƒÐºÑ‚ÑƒÑ€Ñ‹": 7, "kubernetes": 3, "terraform": 3,
        "helm": 2, "ci cd": 1, "infrastructure": 2, "docker": 1,
    },
    "tech_support": {
        "technical support": 7, "tech support": 7, "support engineer": 6,
        "Ñ‚ÐµÑ…Ð½Ð¸Ñ‡ÐµÑÐºÐ°Ñ Ð¿Ð¾Ð´Ð´ÐµÑ€Ð¶ÐºÐ°": 7, "Ñ‚ÐµÑ…Ð¿Ð¾Ð´Ð´ÐµÑ€Ð¶ÐºÐ°": 7,
        "Ð¸Ð½Ð¶ÐµÐ½ÐµÑ€ Ð¿Ð¾Ð´Ð´ÐµÑ€Ð¶ÐºÐ¸": 6, "help desk": 4, "troubleshooting": 3, "ticketing": 2,
    },
}

_ROLE_MARKERS = {
    "backend_python": ("python", "fastapi", "django", "flask", "python backend", "python developer"),
    "data_engineering": ("data engineer", "Ð¸Ð½Ð¶ÐµÐ½ÐµÑ€ Ð´Ð°Ð½Ð½Ñ‹Ñ…", "Ð´Ð°Ñ‚Ð° Ð¸Ð½Ð¶ÐµÐ½ÐµÑ€", "databricks", "data pipeline"),
    "ml_engineering": (
        "machine learning", "ml engineer", "ai engineer", "Ð¸Ð½Ð¶ÐµÐ½ÐµÑ€ Ð¸Ð¸",
        "Ñ€Ð°Ð·Ñ€Ð°Ð±Ð¾Ñ‚Ñ‡Ð¸Ðº Ð¸Ð¸", "Ð¸Ð¸ Ð°Ð³ÐµÐ½Ñ‚", "Ð¸ÑÐºÑƒÑÑÑ‚Ð²ÐµÐ½Ð½Ñ‹Ð¹ Ð¸Ð½Ñ‚ÐµÐ»Ð»ÐµÐºÑ‚", "llm", "ai agents",
    ),
    "devops": ("devops", "sre", "platform engineer", "Ð¸Ð½Ð¶ÐµÐ½ÐµÑ€ Ð¸Ð½Ñ„Ñ€Ð°ÑÑ‚Ñ€ÑƒÐºÑ‚ÑƒÑ€Ñ‹"),
    "tech_support": (
        "technical support", "tech support", "support engineer", "Ñ‚ÐµÑ…Ð½Ð¸Ñ‡ÐµÑÐºÐ°Ñ Ð¿Ð¾Ð´Ð´ÐµÑ€Ð¶ÐºÐ°",
        "Ñ‚ÐµÑ…Ð¿Ð¾Ð´Ð´ÐµÑ€Ð¶ÐºÐ°", "Ð¸Ð½Ð¶ÐµÐ½ÐµÑ€ Ð¿Ð¾Ð´Ð´ÐµÑ€Ð¶ÐºÐ¸", "help desk",
    ),
}


def _normalize(value: str) -> str:
    value = value.casefold().replace("Ñ‘", "Ðµ")
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()


def score_directions(
    title: str,
    description: str,
    weights: Mapping[str, Mapping[str, int]] = DEFAULT_WEIGHTS,
) -> dict[str, int]:
    normalized_title = _normalize(title)
    normalized_description = _normalize(description)
    return {
        direction: sum(
            weight * (2 * (_normalize(keyword) in normalized_title) + (_normalize(keyword) in normalized_description))
            for keyword, weight in keywords.items()
        )
        for direction, keywords in weights.items()
    }


def classify(title: str, description: str, weights: Mapping[str, Mapping[str, int]] = DEFAULT_WEIGHTS) -> str:
    scores = score_directions(title, description, weights)
    combined = _normalize(f"{title} {description}")
    eligible = {
        direction for direction, markers in _ROLE_MARKERS.items()
        if any(_normalize(marker) in combined for marker in markers)
    }
    eligible.update(direction for direction in scores if direction not in _ROLE_MARKERS)
    eligible_scores = {direction: score for direction, score in scores.items() if direction in eligible}
    if not eligible_scores or max(eligible_scores.values()) == 0:
        return "other"
    return max(eligible_scores, key=eligible_scores.get)

