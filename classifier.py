"""Deterministic vacancy classification with no network or model calls."""

from collections.abc import Mapping

DEFAULT_WEIGHTS: dict[str, dict[str, int]] = {
    "backend_python": {"python": 4, "fastapi": 4, "django": 4, "flask": 3, "postgresql": 2, "sqlalchemy": 2, "asyncio": 2, "backend": 2, "rest api": 1},
    "data_engineering": {"data engineer": 5, "airflow": 4, "spark": 4, "etl": 3, "data pipeline": 3, "dbt": 3, "kafka": 2, "warehouse": 2},
    "ml_engineering": {"machine learning": 5, "ml engineer": 5, "pytorch": 4, "tensorflow": 4, "mlops": 3, "model training": 3, "scikit-learn": 2},
    "devops": {"devops": 5, "kubernetes": 4, "terraform": 4, "helm": 3, "ci/cd": 3, "infrastructure": 2, "docker": 1},
}


def score_directions(
    title: str,
    description: str,
    weights: Mapping[str, Mapping[str, int]] = DEFAULT_WEIGHTS,
) -> dict[str, int]:
    normalized_title = title.casefold()
    normalized_description = description.casefold()
    return {
        direction: sum(
            weight * (2 * (keyword.casefold() in normalized_title) + (keyword.casefold() in normalized_description))
            for keyword, weight in keywords.items()
        )
        for direction, keywords in weights.items()
    }


def classify(title: str, description: str, weights: Mapping[str, Mapping[str, int]] = DEFAULT_WEIGHTS) -> str:
    scores = score_directions(title, description, weights)
    if not scores or max(scores.values()) == 0:
        return "other"
    return max(scores, key=scores.get)
