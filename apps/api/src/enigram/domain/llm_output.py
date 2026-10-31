"""Clean-ups for raw model output, independent of the endpoint that produced it."""

import logging
import re

log = logging.getLogger(__name__)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    """Remove Qwen3 reasoning blocks so they never reach extraction or the LLM cache."""
    cleaned = _THINK_BLOCK.sub("", text)
    if "<think>" in cleaned:
        log.warning("Unclosed <think> block in LLM output; dropping everything after it")
        cleaned = cleaned.split("<think>", 1)[0]
    return cleaned.strip()


_DELIM = "<|#|>"
# Keywords are a few short tokens ("included_in", "examines, investigates"), never a sentence.
_MAX_KEYWORD_WORDS = 3


def repair_relation_lines(text: str) -> str:
    """Split relation lines where the model glued keywords and description with a comma.

    Qwen3-8B sometimes writes `relation<|#|>A<|#|>B<|#|>kw, Description...<|#|>`; LightRAG then
    sees 4 fields and silently drops the relation.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith(f"relation{_DELIM}"):
            continue
        fields = line.split(_DELIM)
        if len(fields) != 5 or fields[4].strip() or ", " not in fields[3]:
            continue
        keywords, description = fields[3].split(", ", 1)
        if len(keywords.split()) > _MAX_KEYWORD_WORDS or not description.strip():
            continue
        lines[i] = _DELIM.join([*fields[:3], keywords.strip(), description.strip()])
    return "\n".join(lines)
