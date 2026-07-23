"""Provider-neutral form questions, deterministic matching, and durable answers."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_YES = {"yes", "y", "true", "1", "on", "да", "oui"}
_NO = {"no", "n", "false", "0", "off", "нет", "non"}
_SKIP = {"skip", "blank", "omit", "пропустить", "pass"}
_NUMBERED_LINE = re.compile(r"^\s*(\d+)\s*[.):-]\s*(.*?)\s*$")
SKIPPED = "__jobbot_skip__"


@dataclass(frozen=True)
class FormQuestion:
    provider: str
    field_id: str
    label: str
    input_type: str
    options: tuple[str, ...] = ()
    required: bool = True
    canonical_fact: str = ""
    scope_type: str = "global"
    scope_value: str = ""
    sensitive: bool = False
    confidence: float = 0.0
    invert_boolean: bool = False

    @property
    def answer_key(self) -> tuple[str, str, str]:
        return self.canonical_fact, self.scope_type, self.scope_value

    @property
    def is_boolean(self) -> bool:
        folded = self.input_type.casefold()
        if "bool" in folded or "checkbox" in folded:
            return True
        normalized = {_normalize(option) for option in self.options}
        return bool(normalized) and normalized <= (_YES | _NO)


@dataclass(frozen=True)
class AnswerResolution:
    question: FormQuestion
    canonical_value: Any
    form_value: Any


@dataclass(frozen=True)
class ParsedBatch:
    answers: dict[int, Any]
    errors: tuple[str, ...]


def _normalize(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _exact_scope(provider: str, label: str, input_type: str, options: tuple[str, ...]) -> str:
    signature = json.dumps(
        [provider.casefold(), _normalize(label), input_type.casefold(), [_normalize(v) for v in options]],
        separators=(",", ":"),
    )
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]


def _country_context(label: str, context: dict[str, str]) -> str:
    folded = _normalize(label)
    known = {
        "united states": "United States",
        "u s": "United States",
        "usa": "United States",
        "united kingdom": "United Kingdom",
        "uk": "United Kingdom",
        "france": "France",
        "germany": "Germany",
        "canada": "Canada",
        "georgia": "Georgia",
        "spain": "Spain",
        "italy": "Italy",
        "netherlands": "Netherlands",
        "poland": "Poland",
    }
    for marker, country in known.items():
        if re.search(rf"\b{re.escape(marker)}\b", folded):
            return country
    if "current country" in folded or "country where you" in folded:
        return str(context.get("country") or "").strip()
    job_location = _normalize(context.get("job_country") or "")
    matches = {
        country for marker, country in known.items()
        if re.search(rf"\b{re.escape(marker)}\b", job_location)
    }
    return next(iter(matches)) if len(matches) == 1 else ""


def classify_question(
    provider: str,
    field_id: str,
    label: str,
    input_type: str,
    options: tuple[str, ...] = (),
    required: bool = True,
    *,
    context: dict[str, str] | None = None,
) -> FormQuestion:
    """Classify only high-confidence wording; unknowns use an exact signature."""
    context = context or {}
    text = _normalize(f"{field_id} {label}")
    company = str(context.get("company") or "").strip()
    job_id = str(context.get("job_id") or "").strip()
    country = _country_context(label, context)
    fact = ""
    scope_type = "global"
    scope_value = ""
    sensitive = False
    confidence = 0.99
    invert = False

    if ("first" in text or "given" in text or "prenom" in text) and "name" in text:
        fact = "profile.first_name"
    elif ("last" in text or "family" in text or "surname" in text) and "name" in text:
        fact = "profile.last_name"
    elif text == "name" or text.endswith(" full name") or "full name" in text:
        fact = "profile.full_name"
    elif "email" in text or "e mail" in text:
        fact = "profile.email"
    elif any(word in text for word in ("phone", "mobile", "telephone")):
        fact = "profile.phone"
    elif "linkedin" in text:
        fact = "link.linkedin"
    elif "github" in text:
        fact = "link.github"
    elif any(word in text for word in ("portfolio", "personal website")):
        fact = "link.portfolio"
    elif "sponsor" in text and any(word in text for word in ("future", "later", "eventually", "at any point")):
        fact = "work.requires_sponsorship_future"
        sensitive = True
    elif "sponsor" in text:
        fact = "work.requires_sponsorship_now"
        sensitive = True
        invert = any(phrase in text for phrase in (
            "without sponsorship", "without requiring sponsorship", "without visa sponsorship",
        ))
    elif any(phrase in text for phrase in (
        "authorized to work", "authorised to work", "work authorization",
        "work authorisation", "legally eligible to work", "legal right to work",
        "right to work",
    )):
        fact = "work.authorized"
        sensitive = True
    elif any(phrase in text for phrase in (
        "country will you work", "country will you perform", "working from",
        "work location", "where will you be working",
    )):
        fact = "work.country"
    elif "relocat" in text:
        fact = "work.willing_to_relocate"
    elif any(phrase in text for phrase in (
        "worked for", "worked at", "previously employed by", "ever been employed by",
    )):
        fact = "employment.previously_at_company"
        scope_type, scope_value = "company", _normalize(company)
    elif any(phrase in text for phrase in (
        "how did you hear", "how did you find", "how did you learn",
        "where did you hear", "source of application",
    )):
        fact = "application.source"
    elif any(phrase in text for phrase in (
        "available to start", "availability to start", "when can you start",
        "notice period", "earliest start date",
    )):
        fact = "work.start_availability"
    elif any(word in text for word in ("salary", "compensation", "pay expectation", "desired pay")):
        fact = "application.compensation"
        scope_type, scope_value = "job", job_id
        sensitive = True
    elif any(word in text for word in (
        "gender", "race", "ethnicity", "disability", "veteran", "sexual orientation",
        "pronoun", "marital status", "date of birth", "age",
    )):
        fact = "demographic." + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        sensitive = True
    else:
        fact = "exact." + _exact_scope(provider, label, input_type, options)
        scope_type, scope_value = "provider", provider.casefold()
        confidence = 1.0

    if fact.startswith("work.") and fact not in {"work.country", "work.start_availability"}:
        if country:
            scope_type, scope_value = "country", _normalize(country)
        elif not scope_value:
            scope_type, scope_value = "job", job_id

    return FormQuestion(
        provider=provider,
        field_id=field_id,
        label=label.strip() or field_id,
        input_type=input_type,
        options=tuple(str(option) for option in options),
        required=required,
        canonical_fact=fact,
        scope_type=scope_type,
        scope_value=scope_value,
        sensitive=sensitive,
        confidence=confidence,
        invert_boolean=invert,
    )


def deduplicate_questions(questions: list[FormQuestion] | tuple[FormQuestion, ...]) -> tuple[FormQuestion, ...]:
    result: list[FormQuestion] = []
    seen: set[tuple[str, str, str]] = set()
    for question in questions:
        if question.answer_key in seen:
            continue
        seen.add(question.answer_key)
        result.append(question)
    return tuple(result)


def _boolean_option(options: tuple[str, ...], value: bool) -> str | None:
    candidates = _YES if value else _NO
    matches = [option for option in options if _normalize(option) in candidates]
    return matches[0] if len(matches) == 1 else None


def to_form_value(question: FormQuestion, canonical_value: Any) -> Any:
    if canonical_value == SKIPPED:
        return SKIPPED
    value = canonical_value
    if question.is_boolean and isinstance(value, bool):
        raw_bool = not value if question.invert_boolean else value
        if question.options:
            return _boolean_option(question.options, raw_bool)
        return raw_bool
    if question.options:
        normalized = _normalize(value)
        matches = [option for option in question.options if _normalize(option) == normalized]
        return matches[0] if len(matches) == 1 else None
    return value


def parse_answer_value(question: FormQuestion, text: str) -> tuple[Any, str]:
    value = text.strip()
    folded = _normalize(value)
    if not question.required and folded in _SKIP:
        return SKIPPED, ""
    if question.is_boolean:
        if folded in _YES:
            raw = True
        elif folded in _NO:
            raw = False
        else:
            return None, "answer Yes or No"
        return (not raw if question.invert_boolean else raw), ""
    if question.options:
        if value.isdigit() and 1 <= int(value) <= len(question.options):
            return question.options[int(value) - 1], ""
        matches = [option for option in question.options if _normalize(option) == folded]
        if len(matches) == 1:
            return matches[0], ""
        return None, "choose an option number or exact option label"
    if not value:
        return None, "answer cannot be empty"
    if len(value) > 2000:
        return None, "answer is too long"
    return value, ""


def parse_numbered_answers(text: str, questions: tuple[FormQuestion, ...]) -> ParsedBatch:
    supplied: dict[int, str] = {}
    errors: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _NUMBERED_LINE.match(line)
        if not match:
            errors.append(f"Unrecognized line: {line[:80]}")
            continue
        ordinal = int(match.group(1))
        if ordinal in supplied:
            errors.append(f"Question {ordinal} was answered more than once")
        else:
            supplied[ordinal] = match.group(2)
    parsed: dict[int, Any] = {}
    for ordinal, raw in supplied.items():
        if ordinal < 1 or ordinal > len(questions):
            errors.append(f"Question {ordinal} does not exist")
            continue
        answer, error = parse_answer_value(questions[ordinal - 1], raw)
        if error:
            errors.append(f"Question {ordinal}: {error}")
        else:
            parsed[ordinal] = answer
    for ordinal, question in enumerate(questions, 1):
        if ordinal not in supplied and question.required:
            errors.append(f"Question {ordinal} is required")
        elif ordinal not in supplied:
            parsed[ordinal] = SKIPPED
    return ParsedBatch(parsed, tuple(errors))


_ANSWER_SCHEMA = """
CREATE TABLE IF NOT EXISTS applicant_facts (
    fact_key TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_value TEXT NOT NULL DEFAULT '',
    value_json TEXT NOT NULL,
    sensitive INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (fact_key, scope_type, scope_value)
);
CREATE TABLE IF NOT EXISTS form_question_aliases (
    provider TEXT NOT NULL,
    normalized_label TEXT NOT NULL,
    input_type TEXT NOT NULL,
    option_signature TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_value TEXT NOT NULL,
    confidence REAL NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (provider, normalized_label, input_type, option_signature)
);
CREATE TABLE IF NOT EXISTS answer_batches (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    bot_message_id INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    consent_at TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answer_batch_questions (
    batch_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    question_json TEXT NOT NULL,
    answer_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (batch_id, ordinal)
);
CREATE TABLE IF NOT EXISTS answer_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def ensure_answer_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_ANSWER_SCHEMA)
        connection.commit()
    finally:
        connection.close()


def save_fact(
    db_path: Path,
    question: FormQuestion,
    value: Any,
    *,
    source: str,
) -> None:
    ensure_answer_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """INSERT INTO applicant_facts (
                   fact_key, scope_type, scope_value, value_json, sensitive,
                   source, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(fact_key, scope_type, scope_value) DO UPDATE SET
                   value_json=excluded.value_json, sensitive=excluded.sensitive,
                   source=excluded.source, updated_at=excluded.updated_at""",
            (
                question.canonical_fact, question.scope_type, question.scope_value,
                json.dumps(value, ensure_ascii=False), int(question.sensitive),
                source, now, now,
            ),
        )
        connection.execute(
            """INSERT INTO form_question_aliases (
                   provider, normalized_label, input_type, option_signature,
                   fact_key, scope_type, scope_value, confidence, last_seen_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider, normalized_label, input_type, option_signature)
               DO UPDATE SET fact_key=excluded.fact_key, scope_type=excluded.scope_type,
                   scope_value=excluded.scope_value, confidence=excluded.confidence,
                   last_seen_at=excluded.last_seen_at""",
            (
                question.provider.casefold(), _normalize(question.label),
                question.input_type.casefold(),
                json.dumps([_normalize(item) for item in question.options]),
                question.canonical_fact, question.scope_type, question.scope_value,
                question.confidence, now,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def get_fact(db_path: Path, question: FormQuestion) -> AnswerResolution | None:
    if not db_path.is_file():
        return None
    ensure_answer_schema(db_path)
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            """SELECT value_json FROM applicant_facts
               WHERE fact_key=? AND scope_type=? AND scope_value=?""",
            question.answer_key,
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return None
    canonical = json.loads(row[0])
    form_value = to_form_value(question, canonical)
    if form_value in (None, "", []):
        return None
    return AnswerResolution(question, canonical, form_value)


def forget_fact(db_path: Path, fact_key: str, scope_type: str, scope_value: str) -> bool:
    if not db_path.is_file():
        return False
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.execute(
            "DELETE FROM applicant_facts WHERE fact_key=? AND scope_type=? AND scope_value=?",
            (fact_key, scope_type, scope_value),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def fact_token(question: FormQuestion) -> str:
    raw = "\0".join(question.answer_key)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def forget_fact_by_token(db_path: Path, token: str) -> bool:
    if not db_path.is_file() or not re.fullmatch(r"[0-9a-f]{20}", token):
        return False
    ensure_answer_schema(db_path)
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT fact_key, scope_type, scope_value FROM applicant_facts"
        ).fetchall()
        target = next(
            (
                row for row in rows
                if hashlib.sha256("\0".join(row).encode("utf-8")).hexdigest()[:20] == token
            ),
            None,
        )
        if target is None:
            return False
        cursor = connection.execute(
            "DELETE FROM applicant_facts WHERE fact_key=? AND scope_type=? AND scope_value=?",
            target,
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def migrate_profile_json(db_path: Path, profile_path: Path) -> None:
    """Import applicant.json once. The source file is deliberately left untouched."""
    ensure_answer_schema(db_path)
    connection = sqlite3.connect(db_path)
    try:
        done = connection.execute(
            "SELECT 1 FROM answer_metadata WHERE key='applicant_json_migrated'"
        ).fetchone()
    finally:
        connection.close()
    if done:
        return
    raw: dict[str, Any] = {}
    if profile_path.is_file():
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    for key in ("first_name", "last_name", "email", "phone"):
        if raw.get(key) not in (None, ""):
            question = classify_question("profile", key, key.replace("_", " "), "text")
            save_fact(db_path, question, raw[key], source="applicant_json_migration")
    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    if location:
        for key, value in location.items():
            question = FormQuestion(
                "profile", f"location.{key}", f"Location {key}", "text",
                canonical_fact=f"profile.location.{key}", confidence=1.0,
            )
            save_fact(db_path, question, value, source="applicant_json_migration")
    links = raw.get("links") if isinstance(raw.get("links"), dict) else {}
    for key, value in links.items():
        question = FormQuestion(
            "profile", f"link.{key}", key, "text",
            canonical_fact=f"link.{key}", confidence=1.0,
        )
        save_fact(db_path, question, value, source="applicant_json_migration")
    facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else {}
    for key, value in facts.items():
        question = FormQuestion(
            "profile", f"legacy.{key}", key, "json",
            canonical_fact=f"legacy.{key}", confidence=1.0,
        )
        save_fact(db_path, question, value, source="applicant_json_migration")
    answers = raw.get("answers") if isinstance(raw.get("answers"), dict) else {}
    if answers:
        question = FormQuestion(
            "profile", "legacy.answers", "Legacy answers", "json",
            canonical_fact="legacy.answers", confidence=1.0,
        )
        save_fact(db_path, question, answers, source="applicant_json_migration")
    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """INSERT INTO answer_metadata(key, value, updated_at)
               VALUES ('applicant_json_migrated', '1', ?)""",
            (now,),
        )
        connection.commit()
    finally:
        connection.close()


def profile_document(db_path: Path) -> dict[str, Any]:
    ensure_answer_schema(db_path)
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT fact_key, scope_type, scope_value, value_json FROM applicant_facts"
        ).fetchall()
    finally:
        connection.close()
    raw: dict[str, Any] = {"location": {}, "links": {}, "facts": {}, "answers": {}}
    for fact_key, _scope_type, _scope_value, encoded in rows:
        value = json.loads(encoded)
        if fact_key.startswith("profile.location."):
            raw["location"][fact_key.rsplit(".", 1)[1]] = value
        elif fact_key.startswith("profile."):
            raw[fact_key.split(".", 1)[1]] = value
        elif fact_key.startswith("link."):
            raw["links"][fact_key.split(".", 1)[1]] = value
        elif fact_key.startswith("legacy."):
            legacy_key = fact_key.split(".", 1)[1]
            if legacy_key == "answers" and isinstance(value, dict):
                raw["answers"].update(value)
            else:
                raw["facts"][legacy_key] = value
    return raw


def create_answer_batch(
    db_path: Path,
    job_id: str,
    chat_id: int,
    questions: tuple[FormQuestion, ...],
) -> str:
    ensure_answer_schema(db_path)
    now = datetime.now(timezone.utc)
    batch_id = hashlib.sha256(
        f"{job_id}:{chat_id}:{now.isoformat()}".encode("utf-8")
    ).hexdigest()[:24]
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """INSERT INTO answer_batches (
                   id, job_id, chat_id, status, expires_at, created_at, updated_at
               ) VALUES (?, ?, ?, 'pending', ?, ?, ?)""",
            (batch_id, job_id, chat_id, (now + timedelta(days=7)).isoformat(), now.isoformat(), now.isoformat()),
        )
        connection.executemany(
            """INSERT INTO answer_batch_questions(batch_id, ordinal, question_json)
               VALUES (?, ?, ?)""",
            [
                (batch_id, ordinal, json.dumps(asdict(question), ensure_ascii=False))
                for ordinal, question in enumerate(questions, 1)
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return batch_id


def set_batch_message_id(db_path: Path, batch_id: str, message_id: int) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE answer_batches SET bot_message_id=?, updated_at=? WHERE id=?",
            (message_id, datetime.now(timezone.utc).isoformat(), batch_id),
        )
        connection.commit()
    finally:
        connection.close()


def _question_from_json(encoded: str) -> FormQuestion:
    raw = json.loads(encoded)
    raw["options"] = tuple(raw.get("options") or ())
    return FormQuestion(**raw)


def get_pending_batches(db_path: Path, chat_id: int) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    ensure_answer_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "UPDATE answer_batches SET status='expired' WHERE status='pending' AND expires_at<=?",
            (now,),
        )
        rows = connection.execute(
            """SELECT * FROM answer_batches
               WHERE chat_id=? AND status='pending' ORDER BY created_at""",
            (chat_id,),
        ).fetchall()
        result = []
        for row in rows:
            questions = connection.execute(
                """SELECT ordinal, question_json, answer_json, status
                   FROM answer_batch_questions WHERE batch_id=? ORDER BY ordinal""",
                (row["id"],),
            ).fetchall()
            item = dict(row)
            item["questions"] = tuple(_question_from_json(question[1]) for question in questions)
            result.append(item)
        connection.commit()
        return result
    finally:
        connection.close()


def save_batch_answers(
    db_path: Path,
    batch_id: str,
    questions: tuple[FormQuestion, ...],
    answers: dict[int, Any],
) -> None:
    connection = sqlite3.connect(db_path)
    try:
        for ordinal, value in answers.items():
            connection.execute(
                """UPDATE answer_batch_questions SET answer_json=?, status='answered'
                   WHERE batch_id=? AND ordinal=?""",
                (json.dumps(value, ensure_ascii=False), batch_id, ordinal),
            )
        connection.execute(
            "UPDATE answer_batches SET updated_at=? WHERE id=? AND status='pending'",
            (datetime.now(timezone.utc).isoformat(), batch_id),
        )
        connection.commit()
    finally:
        connection.close()
    for ordinal, value in answers.items():
        if value is not None:
            save_fact(db_path, questions[ordinal - 1], value, source=f"telegram_batch:{batch_id}")


def mark_batch_consented(db_path: Path, batch_id: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.execute(
            """UPDATE answer_batches SET status='consented', consent_at=?, updated_at=?
               WHERE id=? AND status='pending'""",
            (now, now, batch_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def close_batch(db_path: Path, batch_id: str, status: str = "replaced") -> None:
    if status not in {"replaced", "cancelled", "completed"}:
        raise ValueError("Unsupported batch status")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """UPDATE answer_batches SET status=?, updated_at=?
               WHERE id=? AND status='pending'""",
            (status, datetime.now(timezone.utc).isoformat(), batch_id),
        )
        connection.commit()
    finally:
        connection.close()
