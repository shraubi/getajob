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
    messages = [{"role": "user", "content": f"Analyze this job description:\n\n{jd}"}]

    for iteration in range(5):
        response = await llm.chat(
            model=config.GENERATE_MODEL,
            messages=messages,
            system=_SYSTEM,
            tools=ANALYZER_TOOLS,
            temperature=0,
            max_tokens=512,
        )

        tool_calls = llm.extract_tool_calls(response)

        if not tool_calls:
            # Model responded with text instead of tool call — append and retry
            messages.append(llm.build_assistant_turn(response))
            messages.append({
                "role": "user",
                "content": "Please call finish_analysis with your findings.",
            })
            continue

        messages.append(llm.build_assistant_turn(response))

        for tc in tool_calls:
            if tc.function.name == "finish_analysis":
                args = json.loads(tc.function.arguments)
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

    raise AnalyzerError("Job analyzer did not call finish_analysis within iteration limit")
