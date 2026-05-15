# getajob

A Telegram bot that processes job descriptions and generates tailored CVs and recruiter messages using a multi-agent LLM pipeline with RAG.

## Architecture

```
Telegram message (job description)
          │
          ▼
┌─────────────────────────────────────────────────────────────┐
│  Pipeline  (deterministic Python orchestrator)              │
│                                                             │
│  1. score_job()           ← SCORE_MODEL (Haiku / gpt-4o-mini)│
│       └── score < threshold → notify user, stop            │
│                                                             │
│  2. [Agent 1] job_analyzer.analyze()  ← GENERATE_MODEL     │
│       Tool: finish_analysis(required_skills, stack,         │
│             nice_to_have, culture_signals, red_flags,       │
│             role_type)                                      │
│       → JobAnalysis dataclass                               │
│                                                             │
│  3. RAG context enrichment  (parallel fetch)                │
│       ├── ChromaDB: query_profile(required_skills)          │
│       └── ChromaDB: query_applications(role_type)           │
│                                                             │
│  4. [Agent 2] cv_writer.write()  ← GENERATE_MODEL          │
│       Tools: get_profile_sections(topics)  → RAG            │
│              get_past_example(role_type)   → RAG            │
│              finish(cv_text, message, reasoning)            │
│       → ApplicationResult                                   │
│                                                             │
│  5. render_pdf() + log to logs/applications.jsonl           │
└─────────────────────────────────────────────────────────────┘
          │
          ▼
Telegram: PDF + recruiter message + [Send] [Edit] [Skip]
          │
       [Send] → ChromaDB: save_application() for future RAG
```

**Why this architecture:** The pipeline is deterministic Python — the order of steps never changes, so there is no reason to use an LLM as an orchestrator. The two agents are used only where the task is genuinely dynamic: Agent 1 decides *what* to extract from a JD, Agent 2 decides *which* profile sections to emphasize.

## RAG

**candidate_profile collection** — indexed from `cv/base_cv.txt` (or `CV` env var) at startup. The CV is split into chunks by section headers. When Agent 2 runs, it retrieves the most semantically relevant chunks using the required skills from Agent 1's analysis as the query — more precise than querying with the full JD.

**past_applications collection** — populated when the user clicks [Send]. Each saved application is retrieved by semantic similarity to future job descriptions, giving Agent 2 real examples of what worked.

## Multi-LLM model routing

| Task | Default model | Override |
|------|---------------|---------|
| Scoring | `claude-haiku-4-5-20251001` | `SCORE_MODEL=gpt-4o-mini` |
| Agent 1 (Analyzer) | `claude-sonnet-4-6` | `GENERATE_MODEL=gpt-4o` |
| Agent 2 (Writer) | `claude-sonnet-4-6` | `GENERATE_MODEL=gpt-4o` |
| Embeddings | ChromaDB default | `EMBED_MODEL=text-embedding-3-small` + `OPENAI_API_KEY` |

All models route through [LiteLLM](https://github.com/BerriAI/litellm) — no code changes needed to switch providers.

## Observability

Every processed vacancy is appended to `logs/applications.jsonl`:

```json
{
  "ts": "2026-05-11T14:32:01Z",
  "role": "Senior Python Developer",
  "company": "Acme Corp",
  "score": 8,
  "score_model": "claude-haiku-4-5-20251001",
  "generate_model": "claude-sonnet-4-6",
  "rag_profile_chunks": 3,
  "rag_past_apps": 2,
  "analysis": {
    "required_skills": ["FastAPI", "PostgreSQL", "asyncio"],
    "stack": "Python, FastAPI, PostgreSQL",
    "role_type": "backend Python",
    "red_flags": ""
  },
  "writer_reasoning": "Emphasized async Python and high-load PostgreSQL experience given the JD focus on performance",
  "outcome": "pending"
}
```

`outcome` updates to `"sent"` when the user clicks [Send].

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — set TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, YOUR_CHAT_ID
# Replace cv/base_cv.txt with your real CV (or set CV= in .env)

# 3. Run
python main.py
```

## Switching to OpenAI

```bash
# Add to .env:
OPENAI_API_KEY=sk-...
SCORE_MODEL=gpt-4o-mini
GENERATE_MODEL=gpt-4o
EMBED_MODEL=text-embedding-3-small
```

No code changes required.

## Project structure

```
getajob/
├── pipeline.py          # Deterministic orchestrator
├── agents/
│   ├── job_analyzer.py  # Agent 1: structured JD analysis
│   ├── cv_writer.py     # Agent 2: tailored CV + message
│   └── tools.py         # Tool schemas and RAG dispatch
├── rag/
│   ├── store.py         # ChromaDB wrapper
│   └── indexer.py       # CV chunking and indexing
├── llm/
│   └── client.py        # LiteLLM wrapper with retry
├── bot/
│   └── handlers.py      # Telegram event handlers
├── cv/
│   ├── base_cv.txt      # Candidate CV template
│   └── renderer.py      # PDF generation
├── storage/
│   └── state.py         # In-memory session state
└── logs/
    └── applications.jsonl  # Observability log (gitignored)
```
