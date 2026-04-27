import re
import anthropic
import config

_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

_SCORE_SYSTEM = """\
You are a job-fit scorer. Given a job posting, score how well it fits this candidate:

- Python backend developer, 3+ years experience
- Strong: FastAPI, Django, PostgreSQL, Redis, asyncio, REST API design
- Decent: Docker, Celery, SQLAlchemy, pytest
- Some exposure: ML pipelines, data engineering, cloud (AWS/GCP)

Score from 0 to 10. Be strict — only score 7+ if the core stack genuinely matches.

Reply ONLY in this XML format, nothing else:
<score>7</score>
<reason>One sentence explaining the score.</reason>
<role_title>Exact job title from the posting</role_title>
<company>Company name, or Unknown</company>\
"""

_GENERATE_SYSTEM = """\
You are {name} writing a real job application. You write like an intelligent human, not AI.

Rules — these are hard constraints, not suggestions:
- First person, casual-professional tone
- Match the language of the job posting (Russian or English) — do NOT mix languages
- Never use: "passionate", "leverage", "synergy", "excited to", "dynamic team", "results-driven", "I would love to"
- The outreach message MUST reference at least one concrete detail from the job description \
(specific technology, product area, or challenge mentioned)
- Outreach message: under 150 words. If you write more, cut it yourself before replying.
- Tone: someone who has read the JD properly and is genuinely interested, not desperate

Reply ONLY in this XML format:
<cv_text>
Tailored CV text here, max 600 words, plain text
</cv_text>
<message>
Recruiter outreach message here, under 150 words
</message>\
"""


def _extract(tag: str, text: str, fallback: str = "") -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else fallback


async def score_vacancy(jd: str) -> dict:
    resp = await _client.messages.create(
        model=config.SCORE_MODEL,
        max_tokens=256,
        temperature=0,
        system=_SCORE_SYSTEM,
        messages=[{"role": "user", "content": jd}],
    )
    text = resp.content[0].text
    try:
        score = int(_extract("score", text, "0"))
    except ValueError:
        score = 0
    return {
        "score": score,
        "reason": _extract("reason", text, "нет причины"),
        "role_title": _extract("role_title", text, "Developer"),
        "company": _extract("company", text, "Unknown"),
    }


async def generate_application(jd: str, base_cv: str) -> dict:
    name = next((ln.strip() for ln in base_cv.splitlines() if ln.strip()), "Applicant")
    resp = await _client.messages.create(
        model=config.GENERATE_MODEL,
        max_tokens=1500,
        temperature=0.7,
        system=_GENERATE_SYSTEM.format(name=name),
        messages=[
            {
                "role": "user",
                "content": f"Job description:\n{jd}\n\nMy base CV:\n{base_cv}",
            }
        ],
    )
    text = resp.content[0].text
    return {
        "cv_text": _extract("cv_text", text, base_cv),
        "message": _extract("message", text, ""),
    }
