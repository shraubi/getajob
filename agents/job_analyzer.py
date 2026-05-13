import json
import logging
from dataclasses import dataclass
from llm import client as llm
from agents.tools import ANALYZER_TOOLS
import config

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are a job posting analyst. Analyze the provided job description thoroughly, then call \
finish_analysis with your structured findings. Do not write any text response — only call the tool.

Extract:
- required_skills: must-have technical skills explicitly stated or strongly implied
- nice_to_have: preferred/bonus skills
- stack: primary technology stack as a short comma-separated string
- culture_signals: company type, team size, work format, methodology (anything mentioned)
- red_flags: mismatches for a Python backend developer with 3+ years experience (empty string if none)
- role_type: concise category label, e.g. "backend Python", "ML engineer", "fullstack", "DevOps"\
"""

_INITIAL_MSG = "Analyze this job description:\n\n{jd}"


@dataclass
class JobAnalysis:
    required_skills: list[str]
    nice_to_have: list[str]
    stack: str
    culture_signals: str
    red_flags: str
    role_type: str


class AnalyzerError(Exception):
    pass


async def analyze(jd: str) -> JobAnalysis:
    initial_messages = [{"role": "user", "content": _INITIAL_MSG.format(jd=jd)}]
    messages = list(initial_messages)
    last_response_text = ""

    for iteration in range(config.MAX_ANALYZER_ITERATIONS):
        response = await llm.chat(
            model=config.GENERATE_MODEL,
            messages=messages,
            system=_SYSTEM,
            tools=ANALYZER_TOOLS,
            temperature=0,
            max_tokens=512,
        )

        tool_calls = llm.extract_tool_calls(response)
        last_response_text = (response.choices[0].message.content or "") if response.choices else ""

        if not tool_calls:
            logger.warning("Analyzer iteration %d: text response instead of tool call, retrying fresh", iteration)
            # Reset — don't accumulate failed turns in context
            messages = list(initial_messages)
            continue

        for tc in tool_calls:
            if tc.function.name != "finish_analysis":
                continue
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError as e:
                logger.warning("finish_analysis: invalid JSON arguments: %s", e)
                messages = list(initial_messages)
                break

            logger.info(
                "Job analyzed: role_type=%s stack=%s required=%s",
                args.get("role_type"),
                args.get("stack"),
                args.get("required_skills"),
            )
            return JobAnalysis(
                required_skills=args.get("required_skills", []),
                nice_to_have=args.get("nice_to_have", []),
                stack=args.get("stack", ""),
                culture_signals=args.get("culture_signals", ""),
                red_flags=args.get("red_flags", ""),
                role_type=args.get("role_type", "developer"),
            )

    logger.error(
        "Analyzer hit iteration limit (%d). Last response: %s",
        config.MAX_ANALYZER_ITERATIONS,
        last_response_text[:300],
    )
    raise AnalyzerError(
        f"Job analyzer did not call finish_analysis within {config.MAX_ANALYZER_ITERATIONS} iterations"
    )
