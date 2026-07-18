"""Provider-neutral matching for human-visible form field labels."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Sequence

_WORD_RE = re.compile(r"[a-z0-9]+")
_MIN_MATCH_SCORE = 0.90
_MIN_WINNING_MARGIN = 0.03


def normalize_field_label(value: object) -> str:
    """Fold Unicode, accents, punctuation, and whitespace into comparable words."""
    decomposed = unicodedata.normalize("NFKD", str(value))
    unaccented = "".join(
        character for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(_WORD_RE.findall(unaccented.casefold()))


def _contains_phrase(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(haystack[index:index + width] == needle for index in range(len(haystack) - width + 1))


def field_label_score(expected: object, candidate: object) -> float:
    """Return a conservative similarity score suitable for choosing a form field."""
    expected_label = normalize_field_label(expected)
    candidate_label = normalize_field_label(candidate)
    if not expected_label or not candidate_label:
        return 0.0
    if expected_label == candidate_label:
        return 1.0

    expected_words = tuple(expected_label.split())
    candidate_words = tuple(candidate_label.split())
    if _contains_phrase(candidate_words, expected_words) or _contains_phrase(
        expected_words, candidate_words
    ):
        coverage = min(len(expected_words), len(candidate_words)) / max(
            len(expected_words), len(candidate_words)
        )
        return 0.90 + (0.08 * coverage)

    sequence_score = SequenceMatcher(
        None, expected_label, candidate_label
    ).ratio()
    expected_set = set(expected_words)
    candidate_set = set(candidate_words)
    token_coverage = len(expected_set & candidate_set) / max(
        len(expected_set), len(candidate_set)
    )
    if sequence_score >= 0.90 and token_coverage >= 0.80:
        return 0.80 + (0.20 * min(sequence_score, token_coverage))
    return 0.0


def best_field_label_match(
    expected: object,
    candidates: Sequence[object],
) -> int | None:
    """Return the unique best candidate index, or None when absent/ambiguous."""
    ranked = sorted(
        (
            (field_label_score(expected, candidate), index)
            for index, candidate in enumerate(candidates)
        ),
        reverse=True,
    )
    if not ranked or ranked[0][0] < _MIN_MATCH_SCORE:
        return None
    if (
        len(ranked) > 1
        and ranked[1][0] >= _MIN_MATCH_SCORE
        and ranked[0][0] - ranked[1][0] < _MIN_WINNING_MARGIN
    ):
        return None
    return ranked[0][1]
