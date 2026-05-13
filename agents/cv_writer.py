import json
import logging
from dataclasses import dataclass
from llm import client as llm
from agents.tools import WRITER_TOOLS, dispatch_writer_tool
from agents.job_analyzer import JobAnalysis
import config

logger = logging.getLogger(__name__)

_SYSTEM_TEMPLATE = """\
You are writing a job application for {name}. Produce a tailored CV and recruiter message.

Job analysis (pre-computed):
- Required skills: {required_skills}
- Nice to have: {nice_to_have}
- Tech stack: {stack}
- Culture: {culture_signals}
- Red flags: {red_flags}

Available tools:
- get_profile_sections: retrieve relevant candidate profile sections by topic
- get_past_example: retrieve a past application for reference
- finish: submit the final CV and message (required to complete)

Workflow:
1. Call get_profile_sections with the most relevant topics from the required skills
2. Optionally call get_past_example if similar past applications would help with tone
3. Call finish with the tailored CV and recruiter message

Hard rules — violations are unacceptable:
- Write in first person
- Match the language of the job description exactly (Russian JD → Russian output, English JD → English output)
- Never use: "passionate", "leverage", "synergy", "excited to", "dynamic team", "results-driven"
- CV: plain text, max 600 words
- Recruiter message: max 150 words, must reference at least one concrete detail from the JD
- Tone: capable professional who read the JD properly — not desperate, not generic\
"""


@dataclass
class ApplicationResult:
    cv_text: str
    message: str
    reasoning: str


class WriterError(Exception):
    pass


async def write(
    jd: str,
    analysis: JobAnalysis,
    profile_chunks: list[str],
    past_apps: list[dict],
    candidate_name: str = "Candidate",
) -> ApplicationResult:
    system = _SYSTEM_TEMPLATE.format(
        name=candidate_name,
        required_skills=", ".join(analysis.required_skills) or "not specified",
        nice_to_have=", ".join(analysis.nice_to_have) or "none",
        stack=analysis.stack or "not specified",
        culture_signals=analysis.culture_signals or "not specified",
        red_flags=analysis.red_flags or "none",
    )

    pre_context = ""
    if profile_chunks:
        pre_context += "Pre-fetched profile context:\n" + "\n\n---\n\n".join(profile_chunks)
    if past_apps:
        snippets = [
            f"{a['meta'].get('role', 'role')} at {a['meta'].get('company', 'company')}: "
            f"{a['meta'].get('cv_snippet', '')}"
            for a in past_apps
        ]
        pre_context += "\n\nPre-fetched past applications:\n" + "\n\n".join(snippets)

    user_content = f"Job description:\n{jd}"
    if pre_context:
        user_content += f"\n\n{pre_context}"

    messages = [{"role": "user", "content": user_content}]

    for iteration in range(8):
        response = await llm.chat(
            model=config.GENERATE_MODEL,
            messages=messages,
            system=system,
            tools=WRITER_TOOLS,
            temperature=0.7,
            max_tokens=2000,
        )

        tool_calls = llm.extract_tool_calls(response)
        messages.append(llm.build_assistant_turn(response))

        if not tool_calls:
            messages.append({
                "role": "user",
                "content": "Please call the finish tool with your CV and message.",
            })
            continue

        finish_called = False
        for tc in tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)

            if name == "finish":
                logger.info(
                    "CV written: reasoning=%s", args.get("reasoning", "")[:100]
                )
                return ApplicationResult(
                    cv_text=args["cv_text"],
                    message=args["message"],
                    reasoning=args.get("reasoning", ""),
                )

            result = dispatch_writer_tool(name, args)
            messages.append(llm.build_tool_result(tc.id, result))
            finish_called = True

        if not finish_called and not tool_calls:
            messages.append({
                "role": "user",
                "content": "Please call finish to submit the CV and message.",
            })

    raise WriterError("CV writer did not call finish within iteration limit")
