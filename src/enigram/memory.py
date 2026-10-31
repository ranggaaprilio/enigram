"""Agent memories: short notes per API key, searched by meaning."""

from dataclasses import dataclass
from datetime import datetime

MEMORY_MAX_CHARS = 8000  # well under EMBED_MAX_TOKENS
MEMORY_SEARCH_MAX = 20


@dataclass(frozen=True)
class Memory:
    id: int
    text: str
    score: float  # cosine similarity: 1 = same direction
    created_at: datetime


@dataclass(frozen=True)
class MemorySearch:
    memories: list[Memory]
    embed_secs: float
    search_secs: float
