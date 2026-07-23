# Architecture

The active system is intentionally deterministic.

```text
Telegram input
  -> URL extraction and safe page fetch
  -> vacancy parsing
  -> rule-based classification
  -> resume selection
  -> persisted application draft
  -> SQLite-backed reusable form answers
  -> Telegram question batch when required facts are unresolved
  -> explicit confirmation
  -> Hirify, Telegram, or conventional web-form integration
```

## Boundaries

- `jobbot/application.py`: domain parsing, resume extraction, matching, and draft rendering.
- `jobbot/classifier.py`: weighted, explainable direction scores.
- `jobbot/handlers.py`: Telegram orchestration and confirmation gates.
- `jobbot/store.py`: durable job state, idempotency, throttling, and cooldowns.
- `jobbot/form_answers.py`: deterministic question classification, scoped facts,
  numbered-reply parsing, and durable answer batches.
- `jobbot/integrations/`: all network and platform-specific behavior.

The archived LLM/RAG implementation is not imported, installed, built, tested, or deployed.

Answering a Telegram question batch is job-specific submission consent. Stored
facts may be reused on later forms, but later applications still require their
own Apply button unless they present a new answer batch. Sensitive values are
kept out of logs and review events.

