# Architecture

The active system is intentionally deterministic.

```text
Telegram input
  -> URL extraction and safe page fetch
  -> vacancy parsing
  -> rule-based classification
  -> resume selection
  -> persisted application draft
  -> explicit confirmation
  -> Hirify, Telegram, or conventional web-form integration
```

## Boundaries

- `jobbot/application.py`: domain parsing, resume extraction, matching, and draft rendering.
- `jobbot/classifier.py`: weighted, explainable direction scores.
- `jobbot/handlers.py`: Telegram orchestration and confirmation gates.
- `jobbot/store.py`: durable job state, idempotency, throttling, and cooldowns.
- `jobbot/integrations/`: all network and platform-specific behavior.

The archived LLM/RAG implementation is not imported, installed, built, tested, or deployed.
