"""The `agent+repo` baseline (eval/README.md): the same generator as the `code` system, as a
coding agent with read-only tools on the pinned clone. No code from the repository is run."""

import fnmatch
import json
import logging
import os
import re
import time
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from enigram.application.evaluation.golden import GoldenQuestion
from enigram.application.files import atomic_write_text
from enigram.application.ports import ChatModel
from enigram.domain.answer import NO_ANSWER_MARKER, ContextChunk, QueryResult, Source
from enigram.domain.code import parse_code_citations

log = logging.getLogger(__name__)

AGENT_MODE = "agent+repo"
AGENT_MAX_TOOL_CALLS = 30
AGENT_MAX_INPUT_TOKENS = 120_000  # cumulative prompt tokens per question (eval/README.md)
LIST_MAX_ENTRIES, READ_MAX_LINES, GREP_MAX_HITS = 200, 200, 50
FILE_MAX_BYTES = 1_000_000  # like repo_sync.REPO_MAX_FILE_BYTES
GREP_MAX_LINE_CHARS = 200
SKIP_DIRS = {".git", "node_modules", "dist", "build"}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a directory of the repository (at most 200 entries).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file with 1-based numbered lines (at most 200 lines per call).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a regular expression (at most 50 hits as "
            "path:line:text).",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "glob": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
]
SYSTEM_PROMPT = (
    "You are a coding agent answering a question about a TypeScript repository checked out at "
    "commit {sha}. Use the tools to read the code; do not guess. Answer in 1-5 sentences; every "
    "sentence ends with a citation `path:line@{sha12}` (or `path:start-end@{sha12}`) of the code "
    "it relies on. If the repository does not answer the question, say so in one sentence "
    f"ending with {NO_ANSWER_MARKER}."
)
BUDGET_MESSAGE = "Tool budget exhausted. Answer now from what you have read."


class RepoTools:
    """Read-only tools confined to one clone; every problem is returned as text, never raised
    to the model."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _safe(self, rel: str) -> Path | str:
        """The path under the root, or an error text: no escaping the clone, no symbolic links,
        no skipped directories."""
        if not isinstance(rel, str):
            return "path must be a string"
        parts = Path(rel.lstrip("/")).parts
        if any(part in SKIP_DIRS for part in parts):
            return f"{rel}: not readable"
        probe = self.root
        for part in parts:
            probe = probe / part
            if probe.is_symlink():
                return f"{rel}: not readable (symbolic link)"
        resolved = (self.root / Path(*parts)).resolve() if parts else self.root
        if not resolved.is_relative_to(self.root):
            return f"{rel}: outside the repository"
        return resolved

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def list_dir(self, path: str = ".") -> str:
        target = self._safe(path or ".")
        if isinstance(target, str):
            return target
        if not target.is_dir():
            return f"{path}: not a directory"
        entries = sorted(
            f"{p.name}/" if p.is_dir() else p.name
            for p in target.iterdir()
            if p.name not in SKIP_DIRS and not p.is_symlink()
        )
        more = len(entries) - LIST_MAX_ENTRIES
        shown = entries[:LIST_MAX_ENTRIES]
        return "\n".join(shown + ([f"… {more} more"] if more > 0 else [])) or "(empty)"

    def read_file(self, path: str, start: int | None = None, end: int | None = None) -> str:
        target = self._safe(path)
        if isinstance(target, str):
            return target
        if not target.is_file():
            return f"{path}: not a file"
        if target.stat().st_size > FILE_MAX_BYTES:
            return f"{path}: larger than {FILE_MAX_BYTES} bytes"
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        first = max(1, int(start or 1))
        last = min(len(lines), int(end or first + READ_MAX_LINES - 1), first + READ_MAX_LINES - 1)
        if first > len(lines):
            return f"{path} has {len(lines)} lines"
        body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(first, last + 1))
        tail = f"\n… lines {last + 1}-{len(lines)} not shown" if last < len(lines) else ""
        return body + tail

    def grep(self, pattern: str, glob: str | None = None) -> str:
        try:
            regex = re.compile(pattern)
        except (re.error, TypeError) as e:
            return f"invalid regular expression: {e}"
        hits: list[str] = []
        for folder, dirs, names in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(names):
                path = Path(folder) / name
                rel = self._rel(path)
                if path.is_symlink() or (glob and not fnmatch.fnmatch(rel, glob)):
                    continue
                try:
                    if path.stat().st_size > FILE_MAX_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for n, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{rel}:{n}:{line.strip()[:GREP_MAX_LINE_CHARS]}")
                        if len(hits) >= GREP_MAX_HITS:
                            return "\n".join(hits) + f"\n… stopped at {GREP_MAX_HITS} hits"
        return "\n".join(hits) or "no matches"

    def run(self, name: str, args: dict[str, Any]) -> str:
        try:
            if name == "list_dir":
                return self.list_dir(args.get("path", "."))
            if name == "read_file":
                return self.read_file(args["path"], args.get("start"), args.get("end"))
            if name == "grep":
                return self.grep(args["pattern"], args.get("glob"))
        except KeyError as e:
            return f"missing argument {e}"
        except (TypeError, ValueError) as e:
            return f"bad arguments: {e}"
        return f"unknown tool {name!r}"


def _result_from_json(data: dict[str, Any]) -> QueryResult:
    return QueryResult(
        **{
            **data,
            "sources": [Source(**s) for s in data["sources"]],
            "chunks": [ContextChunk(**c) for c in data["chunks"]],
            "entities": [],
            "relations": [],
        }
    )


class AgentRepoRunner:
    def __init__(
        self,
        chat: ChatModel,
        root: Path,
        sha: str,
        cache_dir: Path | None = None,
        *,
        max_tool_calls: int = AGENT_MAX_TOOL_CALLS,
        max_input_tokens: int = AGENT_MAX_INPUT_TOKENS,
        model: str = "",
    ) -> None:
        self.chat, self.sha, self.cache_dir = chat, sha, cache_dir
        self.tools = RepoTools(root)
        self.max_tool_calls, self.max_input_tokens, self.model = (
            max_tool_calls,
            max_input_tokens,
            model,
        )

    def _cache_path(self, question: str) -> Path | None:
        if self.cache_dir is None:
            return None
        key = sha256(f"{question}\n{self.sha}\n{self.model}".encode()).hexdigest()
        return self.cache_dir / f"{key}.json"

    async def answer(self, q: GoldenQuestion) -> QueryResult:
        path = self._cache_path(q.question)
        if path and path.exists():
            log.info("[agent] %s: cached", q.id)
            return _result_from_json(json.loads(path.read_text(encoding="utf-8")))
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(sha=self.sha, sha12=self.sha[:12])},
            {"role": "user", "content": q.question},
        ]
        calls = tokens_in = tokens_out = 0
        trace: list[dict[str, Any]] = []
        exhausted = False
        start = time.perf_counter()
        while True:
            reply = await self.chat.chat(messages, tools=TOOLS, temperature=0.0)
            tokens_in += reply.tokens_in
            tokens_out += reply.tokens_out
            if not reply.tool_calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.text or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.arguments},
                        }
                        for c in reply.tool_calls
                    ],
                }
            )
            for c in reply.tool_calls:
                calls += 1
                try:
                    args = json.loads(c.arguments) if c.arguments else {}
                except json.JSONDecodeError:
                    args = None
                out = (
                    "invalid JSON arguments"
                    if not isinstance(args, dict)
                    else self.tools.run(c.name, args)
                )
                trace.append({"tool": c.name, "args": args, "chars": len(out)})
                messages.append({"role": "tool", "tool_call_id": c.id, "content": out})
            # Checked after each round, so the first call always happens.
            if calls >= self.max_tool_calls or tokens_in >= self.max_input_tokens:
                exhausted = True
                messages.append({"role": "user", "content": BUDGET_MESSAGE})
                reply = await self.chat.chat(messages, temperature=0.0)  # no tools: answer now
                tokens_in += reply.tokens_in
                tokens_out += reply.tokens_out
                break
        text = reply.text
        found = NO_ANSWER_MARKER not in text
        text = text.replace(NO_ANSWER_MARKER, "").strip()
        sources = [
            Source(
                ref=i,
                title=c.file,
                section="",
                pages="",
                citation=f"{c.file}:{c.start}-{c.end}@{c.sha}",
            )
            for i, c in enumerate(parse_code_citations(text), 1)
        ]
        result = QueryResult(
            q.question,
            text,
            sources,
            [],
            found=found,
            query_secs=round(time.perf_counter() - start, 1),
            mode=AGENT_MODE,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            extra={"tool_calls": calls, "trace": trace, "budget_exhausted": exhausted},
        )
        log.info(
            "[agent] %s: %d tool calls, %d in / %d out tokens%s",
            q.id,
            calls,
            tokens_in,
            tokens_out,
            " (budget exhausted)" if exhausted else "",
        )
        if path:
            atomic_write_text(path, json.dumps(asdict(result), ensure_ascii=False))
        return result
