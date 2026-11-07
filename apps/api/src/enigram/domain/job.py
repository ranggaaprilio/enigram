"""Jobs the API queues for the index worker."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

ACTIVE = ("queued", "running")  # then done | failed


@dataclass(frozen=True)
class Job:
    id: int
    kind: str  # "index"
    payload: dict[str, Any]  # {"book": ..., "file": ...}
    status: str
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    workspace: str  # the workspace whose adapters run it
