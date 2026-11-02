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

from enigram.application.agent_loop import (
    AGENT_MAX_INPUT_TOKENS,
    AGENT_MAX_TOOL_CALLS,
    BUDGET_MESSAGE,
    FILE_MAX_BYTES,
    GREP_MAX_HITS,
    GREP_MAX_LINE_CHARS,
    LIST_MAX_ENTRIES,
    READ_MAX_LINES,
    SKIP_DIRS,
    loop_answer,
    result_from_json,
    run_tool_loop,
)
from enigram.application.evaluation.golden import GoldenQuestion
from enigram.application.files import atomic_write_text
from enigram.application.ports import ChatModel
from enigram.domain.answer import NO_ANSWER_MARKER, QueryResult
from enigram.domain.secrets import is_secret_path, redact

log = logging.getLogger(__name__)

AGENT_MODE = "agent+repo"
__all__ = ["AGENT_MODE", "BUDGET_MESSAGE", "TOOLS", "AgentRepoRunner", "RepoTools"]
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


class RepoTools:
    """Read-only tools confined to one clone; every problem is returned as text, never raised
    to the model. Like the code index, secret files are not read and secret values are
    redacted: the model is external, and both systems must see the same inputs."""

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
        if parts and is_secret_path(Path(*parts).as_posix()):
            return f"{rel}: not readable (secret file)"
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
        return redact(body)[0] + tail

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
                if (
                    path.is_symlink()
                    or is_secret_path(rel)
                    or (glob and not fnmatch.fnmatch(rel, glob))
                ):
                    continue
                try:
                    if path.stat().st_size > FILE_MAX_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for n, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        line = redact(line.strip())[0][:GREP_MAX_LINE_CHARS]
                        hits.append(f"{rel}:{n}:{line}")
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
            return result_from_json(json.loads(path.read_text(encoding="utf-8")))
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(sha=self.sha, sha12=self.sha[:12])},
            {"role": "user", "content": q.question},
        ]
        start = time.perf_counter()

        async def run(name: str, args: dict[str, Any]) -> str:
            return self.tools.run(name, args)

        loop = await run_tool_loop(
            self.chat,
            messages,
            TOOLS,
            run,
            max_tool_calls=self.max_tool_calls,
            max_input_tokens=self.max_input_tokens,
        )
        result = loop_answer(
            q.question, loop, mode=AGENT_MODE, secs=time.perf_counter() - start, extra={}
        )
        log.info(
            "[agent] %s: %d tool calls, %d in / %d out tokens%s",
            q.id,
            loop.calls,
            loop.tokens_in,
            loop.tokens_out,
            " (budget exhausted)" if loop.exhausted else "",
        )
        if path:
            atomic_write_text(path, json.dumps(asdict(result), ensure_ascii=False))
        return result
