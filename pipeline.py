import asyncio
import json
import logging
import os
import pathlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from llm import client as llm
from rag import store as rag_store
from agents import job_analyzer, cv_writer
from agents.job_analyzer import JobAnalysis
import config

logger = logging.getLogger(__name__)

_SCORE_SYSTEM = """\
You are a job-fit scorer. Given a job posting, score how well it fits this candidate:

- Python backend developer, 3+ years experience
- Strong: FastAPI, Django, PostgreSQL, Redis, asyncio, REST API design
- Decent: Docker, Celery, SQLAlchemy, pytest
- Some exposure: ML pipelines, data engineering, cloud (AWS/GCP)

Score 0–10. Be strict — only 7+ if the core stack genuinely matches.

Reply ONLY in this XML format, nothing else:
<score>7</score>
<reason>One sentence explaining the score.</reason>
<role_title>Exact job title from the posting</role_title>
<company>Company name, or Unknown</company>\
"""

_LOG_PATH = pathlib.Path(__file__).parent / "logs" / "applications.jsonl"


@dataclass
class ScoreResult:
    score: int
    reason: str
    role_title: str
    company: str


@dataclass
class PipelineResult:
    action: str  # "generate" or "skip"
    score: int
    reason: str
    role_title: str
    company: str
    cv_text: str = ""
    message: str = ""
    reasoning: str = ""
    analysis: JobAnalysis | None = None


def _extract(tag: str, text: str, fallback: str = "") -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else fallback


def _candidate_name() -> str:
    cv_text = os.environ.get("CV", "")
    if not cv_text:
        cv_path = pathlib.Path(__file__).parent / "cv" / "base_cv.txt"
        cv_text = cv_path.read_text(encoding="utf-8")
    first_line = next((ln.strip() for ln in cv_text.splitlines() if ln.strip()), "")
    return first_line or "Candidate"


async def _score(jd: str) -> ScoreResult:
    response = await llm.chat(
        model=config.SCORE_MODEL,
        messages=[{"role": "user", "content": jd}],
        system=_SCORE_SYSTEM,
        temperature=0,
        max_tokens=256,
    )
    text = response.choices[0].message.content or ""
    try:
        score = int(_extract("score", text, "0"))
    except ValueError:
        score = 0
    return ScoreResult(
        score=score,
        reason=_extract("reason", text, ""),
        role_title=_extract("role_title", text, "Developer"),
        company=_extract("company", text, "Unknown"),
    )


def _log(entry: dict) -> None:
    try:
        _LOG_PATH.parent.mkdir(exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("Failed to write log entry: %s", exc)


async def run(jd: str) -> PipelineResult:
    # Step 1: score with cheap model
    score_result = await _score(jd)
    logger.info(
        "Score: %d/10 — %s at %s (model: %s)",
        score_result.score, score_result.role_title, score_result.company, config.SCORE_MODEL,
    )

    if score_result.score < config.SCORE_THRESHOLD:
        _log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "role": score_result.role_title,
            "company": score_result.company,
            "score": score_result.score,
            "score_model": config.SCORE_MODEL,
            "outcome": "skipped_low_score",
        })
        return PipelineResult(
            action="skip",
            score=score_result.score,
            reason=score_result.reason,
            role_title=score_result.role_title,
            company=score_result.company,
        )

    # Step 2: Agent 1 — analyze JD structure
    analysis = await job_analyzer.analyze(jd)

    # Step 3: RAG — enrich context using analysis (parallel fetch)
    rag_query = " ".join(analysis.required_skills) if analysis.required_skills else jd[:200]
    profile_chunks, past_apps = await asyncio.gather(
        asyncio.to_thread(rag_store.query_profile, rag_query, 3),
        asyncio.to_thread(rag_store.query_applications, analysis.role_type, 2),
    )

    logger.info(
        "RAG: retrieved %d profile chunks, %d past applications",
        len(profile_chunks), len(past_apps),
    )

    # Step 4: Agent 2 — write CV + message
    result = await cv_writer.write(
        jd=jd,
        analysis=analysis,
        profile_chunks=profile_chunks,
        past_apps=past_apps,
        candidate_name=_candidate_name(),
    )

    # Step 5: log
    _log({
        "ts": datetime.now(timezone.utc).isoformat(),
        "role": score_result.role_title,
        "company": score_result.company,
        "score": score_result.score,
        "score_model": config.SCORE_MODEL,
        "generate_model": config.GENERATE_MODEL,
        "rag_profile_chunks": len(profile_chunks),
        "rag_past_apps": len(past_apps),
        "analysis": {
            "required_skills": analysis.required_skills,
            "stack": analysis.stack,
            "role_type": analysis.role_type,
            "red_flags": analysis.red_flags,
        },
        "writer_reasoning": result.reasoning,
        "outcome": "pending",
    })

    return PipelineResult(
        action="generate",
        score=score_result.score,
        reason=score_result.reason,
        role_title=score_result.role_title,
        company=score_result.company,
        cv_text=result.cv_text,
        message=result.message,
        reasoning=result.reasoning,
        analysis=analysis,
    )
