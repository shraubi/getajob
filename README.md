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
pip install -r requirements.txt  # lightweight token-free development
# For the legacy LLM/RAG path instead: pip install -r requirements-legacy.txt

# 2. Configure
cp .env.example .env
# Set TELEGRAM_BOT_TOKEN and YOUR_CHAT_ID

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


## Token-free Telegram mode

This mode accepts a pasted vacancy, classifies it with local weighted rules, automatically classifies every PDF resume found on the VM from its extracted text and filename, and returns the best matching resume plus a deterministic recruiter message. It makes no LLM or embedding calls.

Use this input format for the best metadata extraction:

```text
Title: Senior Python Engineer
Company: Acme
https://jobs.example/42

Full vacancy description...
```

Deploy private résumés on the VM (they are gitignored):

```text
data/resumes/
├── my_python_cv.pdf
├── platform_resume.pdf
└── data_cv.pdf
```

Configure and start:

```bash
cp .env.example .env
# Set TELEGRAM_BOT_TOKEN and YOUR_CHAT_ID, then:
TOKEN_FREE_MODE=true
RESUME_DIR=data/resumes
python main.py
```

PDF filenames are not configuration: normal descriptive names help as a fallback for scanned PDFs, while text-based PDFs are classified from their first five pages. The bot fails safely when the vacancy is unknown or no uploaded PDF matches its direction. Set `TOKEN_FREE_MODE=false` to retain the legacy LLM/RAG flow during rollout.


For the Docker Compose deployment, put the PDFs in `data/resumes/` on the VM. Compose mounts that directory read-only at `/app/data/resumes`. Rebuild and restart with:

```bash
docker compose up -d --build
```


### Dependencies

`requirements.txt` is the lightweight default used by local development, CI, Docker, and automatic deployment. `requirements-legacy.txt` layers the old LLM/RAG dependencies on top and is only needed when running with `TOKEN_FREE_MODE=false`.


## Generic linked job pages

When a Telegram message contains a public HTTP(S) URL, the bot treats the linked page—not the Telegram preview—as the source of truth. It validates redirect targets, limits redirects/page size/time, rejects private-network destinations, and parses in this order:

1. Schema.org JSON-LD `JobPosting`
2. OpenGraph and standard metadata
3. Semantic `h1`, `main`, or `article` content

Apply/contact targets are discovered generically from forms, resume/CV fields, and link/button labels such as apply, application, submit, contact, send CV, and their common Russian equivalents. There are no per-site CSS selector tables.

The response includes the detected page category, final fetched URL, apply/contact URL when found, classification, selected resume, and generated message. Expired pages (HTTP 404/410) are reported and are not classified from Telegram preview text.
