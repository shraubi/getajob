"""In-memory review models and transcript-free report serialization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ChatMessage:
    id: int
    date: datetime
    outgoing: bool
    text: str = ""
    has_document: bool = False
    buttons: tuple[str, ...] = ()
    edit_date: datetime | None = None
    reply_to_message_id: int | None = None


@dataclass(frozen=True)
class Interaction:
    id: str
    request: ChatMessage | None
    responses: tuple[ChatMessage, ...]

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        return ((self.request,) if self.request else ()) + self.responses


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: str
    summary: str
    interaction_id: str
    message_ids: tuple[int, ...]
    timestamps: tuple[str, ...]
    evidence: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "summary": self.summary,
            "interaction_id": self.interaction_id,
            "message_ids": list(self.message_ids),
            "timestamps": list(self.timestamps),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ReviewReport:
    id: str
    peer_key: str
    marker_message_id: int | None
    marker_run_id: str | None
    start_message_id: int
    end_message_id: int
    analyzed_messages: int
    findings: tuple[Finding, ...]
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "review_run_id": self.id,
            "peer_key": self.peer_key,
            "boundary": {
                "marker_message_id": self.marker_message_id,
                "marker_run_id": self.marker_run_id,
                "start_message_id": self.start_message_id,
                "end_message_id": self.end_message_id,
            },
            "analyzed_messages": self.analyzed_messages,
            "finding_count": len(self.findings),
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }
