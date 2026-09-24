"""Production bridge from stored proposal outbox payloads to Studio events."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class StudioOutboxAppender:
    studio: Any

    def append_proposal_event(self, outbox_id: str, payload: Mapping[str, Any]) -> int:
        return self.studio.append_outbox_event(outbox_id, dict(payload))


__all__ = ["StudioOutboxAppender"]
