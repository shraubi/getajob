"""Structured operational journal consumed by standalone Ralph."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobbot_review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interaction_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobbot_review_events_interaction
ON jobbot_review_events(interaction_id, id);
"""

def record_review_event(db_path: Path, interaction_id: str, event_type: str, **data: object) -> int:
    """Append normalized behavior data. Callers must never pass transcript text."""
    if not interaction_id:
        return 0
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    try:
        connection.executescript(_SCHEMA)
        cursor = connection.execute(
            """INSERT INTO jobbot_review_events
               (interaction_id, event_type, occurred_at, data_json)
               VALUES (?, ?, ?, ?)""",
            (interaction_id, event_type, datetime.now(timezone.utc).isoformat(),
             json.dumps(data, ensure_ascii=False, sort_keys=True)),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()
