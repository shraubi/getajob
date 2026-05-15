import json
from rag import store as rag_store

# ---------------------------------------------------------------------------
# Analyzer tools — Agent 1 uses these to submit structured JD analysis
# ---------------------------------------------------------------------------

ANALYZER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "finish_analysis",
            "description": (
                "Submit the completed job analysis. This is the only way to finish. "
                "Call this once you have extracted all relevant information from the job description."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "required_skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Must-have technical skills and experience (from JD requirements)",
                    },
                    "nice_to_have": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Preferred but not required skills",
                    },
                    "stack": {
                        "type": "string",
                        "description": "Primary tech stack, comma-separated (e.g. 'Python, FastAPI, PostgreSQL')",
                    },
                    "culture_signals": {
                        "type": "string",
                        "description": "Company/team culture indicators: startup vs enterprise, remote/hybrid/office, team size, methodology",
                    },
                    "red_flags": {
                        "type": "string",
                        "description": "Potential concerns for the candidate: seniority mismatch, domain gap, unusual requirements. Empty string if none.",
                    },
                    "role_type": {
                        "type": "string",
                        "description": "Short role category label, e.g. 'backend Python', 'ML engineer', 'fullstack'",
                    },
                },
                "required": ["required_skills", "nice_to_have", "stack", "culture_signals", "red_flags", "role_type"],
            },
        },
    }
]

# ---------------------------------------------------------------------------
# Writer tools — Agent 2 uses these to retrieve context and submit output
# ---------------------------------------------------------------------------

WRITER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_profile_sections",
            "description": "Retrieve relevant sections from the candidate profile by topic. Use this to get specific skills, experience, or background relevant to the job.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Skills or areas to look up, e.g. ['FastAPI', 'PostgreSQL', 'async Python']",
                    }
                },
                "required": ["topics"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_past_example",
            "description": "Retrieve a past application for a similar role as reference for tone and emphasis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role_type": {
                        "type": "string",
                        "description": "Role category to search for, e.g. 'backend Python'",
                    }
                },
                "required": ["role_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Submit the final tailored CV and recruiter message. "
                "This is the only way to complete the task — must be called once."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cv_text": {
                        "type": "string",
                        "description": "Tailored CV text, max 600 words, plain text",
                    },
                    "message": {
                        "type": "string",
                        "description": "Recruiter outreach message, max 150 words, references a concrete JD detail",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "One sentence explaining what profile aspects were emphasized and why",
                    },
                },
                "required": ["cv_text", "message", "reasoning"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def dispatch_writer_tool(name: str, args: dict) -> str:
    if name == "get_profile_sections":
        topics = args.get("topics", [])
        query = " ".join(topics) if isinstance(topics, list) else str(topics)
        chunks = rag_store.query_profile(query, n=3)
        if not chunks:
            return "No relevant profile sections found."
        return "\n\n---\n\n".join(chunks)

    if name == "get_past_example":
        role_type = args.get("role_type", "")
        apps = rag_store.query_applications(role_type, n=1)
        if not apps:
            return "No past examples found."
        app = apps[0]
        meta = app["meta"]
        return (
            f"Past application — {meta.get('role', 'similar role')} at {meta.get('company', 'unknown')}:\n"
            f"{meta.get('cv_snippet', '')}"
        )

    raise ValueError(f"Unknown writer tool: {name!r}")
