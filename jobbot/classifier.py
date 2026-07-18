"""Deterministic multilingual, role-first vacancy classification."""

from collections.abc import Mapping
import re

DEFAULT_WEIGHTS: dict[str, dict[str, int]] = {
    "backend_python": {
        "python": 6, "fastapi": 6, "django": 6, "flask": 5,
        "sqlalchemy": 3, "asyncio": 2, "backend": 2, "бекенд": 2, "бэкенд": 2,
    },
    "data_engineering": {
        "data engineer": 7, "инженер данных": 7, "дата инженер": 7,
        "databricks": 5, "airflow": 4, "spark": 4, "etl": 3,
        "data pipeline": 3, "пайплайн данных": 3, "dbt": 3, "warehouse": 2,
    },
    "ml_engineering": {
        "machine learning": 7, "ml engineer": 7, "ai engineer": 7,
        "инженер ии": 7, "разработчик ии": 7, "ии агент": 6,
        "искусственный интеллект": 6, "llm": 5, "ai agents": 5,
        "pytorch": 4, "tensorflow": 4, "mlops": 3,
    },
    "devops": {
        "devops": 8, "sre": 7, "platform engineer": 7,
        "инженер инфраструктуры": 7, "kubernetes": 3, "terraform": 3,
        "helm": 2, "ci cd": 1, "infrastructure": 2, "docker": 1,
    },
    "tech_support": {
        "technical support": 7, "tech support": 7, "support engineer": 6,
        "customer support": 6, "payment support": 8, "support manager": 6,
        "техническая поддержка": 7, "технической поддержки": 8, "техподдержка": 7,
        "специалист поддержки": 7, "специалист технической поддержки": 9,
        "инженер поддержки": 6, "help desk": 4, "troubleshooting": 3, "ticketing": 2,
    },
}

_ROLE_MARKERS = {
    "backend_python": ("python", "fastapi", "django", "flask", "python backend", "python developer"),
    "data_engineering": ("data engineer", "инженер данных", "дата инженер", "databricks", "data pipeline"),
    "ml_engineering": (
        "machine learning", "ml engineer", "ai engineer", "инженер ии",
        "разработчик ии", "ии агент", "искусственный интеллект", "llm", "ai agents",
    ),
    "devops": ("devops", "sre", "platform engineer", "инженер инфраструктуры"),
    "tech_support": (
        "technical support", "tech support", "support engineer", "customer support", "payment support",
        "support manager", "техническая поддержка", "технической поддержки", "техподдержка",
        "специалист поддержки", "специалист технической поддержки", "инженер поддержки", "help desk",
    ),
}

_UNSUPPORTED_TITLE_STACKS = (
    "typescript", "javascript", "nestjs", "node js", "nodejs",
    "android", "kotlin", "ios", "swift", "java", "golang", "dotnet", "net developer", "c sharp",
)


def _normalize(value: str) -> str:
    value = value.casefold().replace("ё", "е")
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
    normalized_title = _normalize(title)
    combined = _normalize(f"{title} {description}")
    eligible = {
        direction for direction, markers in _ROLE_MARKERS.items()
        if any(_normalize(marker) in combined for marker in markers)
    }
    eligible.update(direction for direction in scores if direction not in _ROLE_MARKERS)
    if any(_normalize(marker) in normalized_title for marker in _UNSUPPORTED_TITLE_STACKS):
        eligible = {
            direction for direction in eligible
            if any(_normalize(marker) in normalized_title for marker in _ROLE_MARKERS.get(direction, ()))
        }
    eligible_scores = {direction: score for direction, score in scores.items() if direction in eligible}
    if not eligible_scores or max(eligible_scores.values()) == 0:
        return "other"
    return max(eligible_scores, key=eligible_scores.get)
