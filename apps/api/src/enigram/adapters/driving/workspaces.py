"""Adapters per workspace, for the API (the X-Enigram-Workspace header picks one set, no header
the process workspace: WORKSPACE, `default` unless set) and the worker (the workspace of each
job). Built on first use and kept until shutdown; the repositories share the job store's pool."""

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from enigram.application.ports import (
    AnnotationRepository,
    ChatRepository,
    CodeEmbedder,
    CodeGraphRepository,
    ConflictRepository,
    DecisionProvider,
    Deps,
    JobRepository,
    KnowledgeEngine,
    MemoryRepository,
)
from enigram.config import Config
from enigram.domain.errors import NotFound
from enigram.domain.workspace import check_workspace_name

WORKSPACE_HEADER = "x-enigram-workspace"


@dataclass
class Workspace:
    cfg: Config  # cfg.workspace is the name, cfg.data_dir holds its files
    deps: Deps
    store: JobRepository  # the job queue, query log and daily budget
    memory: MemoryRepository
    code_graph: CodeGraphRepository
    # Docs-vs-code results, written by the worker's detect_conflicts job (FK to code_repos).
    conflicts: ConflictRepository
    # Written synchronously by the annotation routes; the worker writes staleness flags.
    notes: AnnotationRepository
    # The decision layer: the abstain guard on /query, conflict checks on verification.
    decider: DecisionProvider
    code_embedder: CodeEmbedder  # search_code's semantic channel on /query and MCP
    chat: ChatRepository  # chat sessions and messages (plan item 8)
    engine: KnowledgeEngine | None = None  # the home one opens at start, others on first use


class Workspaces:
    def __init__(
        self,
        cfg: Config,
        deps: Deps,
        deps_for: Callable[[Config], Deps],
        store: JobRepository,
        stack: AsyncExitStack,
    ):
        self.cfg, self.deps, self.deps_for = cfg, deps, deps_for
        self.store, self.stack = store, stack
        self.open: dict[str, Workspace] = {}
        # One build or engine open at a time. Not re-entrant: get() must never call engine().
        self.lock = asyncio.Lock()

    @property
    def home(self) -> Workspace:
        return self.open[self.cfg.workspace]

    async def start(self) -> Workspace:
        """Registers the process workspace and opens its adapters and engine, as the API did
        before workspaces."""
        await self.store.add_workspace(self.cfg.workspace)
        home = await _build(self.cfg, self.deps, self.store)
        self.open[self.cfg.workspace] = home
        await self.engine(home)
        return home

    async def get(self, name: str | None) -> Workspace:
        name = name or self.cfg.workspace
        if found := self.open.get(name):
            return found
        async with self.lock:
            if found := self.open.get(name):
                return found
            if name not in {w["name"] for w in await self.store.list_workspaces()}:
                raise NotFound(f"No workspace {name!r}")
            if name == self.cfg.workspace:  # the worker builds it here, the API in start()
                ws = await _build(self.cfg, self.deps, self.store)
            else:
                cfg = self.cfg.for_workspace(name)
                ws = await _build(cfg, self.deps_for(cfg), self.store.for_workspace(name))
            self.open[name] = ws
            return ws

    async def engine(self, ws: Workspace) -> KnowledgeEngine:
        if ws.engine is None:
            async with self.lock:
                if ws.engine is None:
                    # require_index=False: a new workspace (or deployment) has no index yet.
                    ws.engine = await self.stack.enter_async_context(
                        ws.deps.open_engine(query_cache=False, require_index=False)
                    )
        return ws.engine

    async def list(self) -> list[dict[str, Any]]:
        return await self.store.list_workspaces()

    async def create(self, name: str) -> dict[str, Any] | None:
        """None when it already exists; raises EnigramError for a bad name."""
        return await self.store.add_workspace(check_workspace_name(name))


async def _build(cfg: Config, deps: Deps, store: JobRepository) -> Workspace:
    # Conflicts after the code graph: its tables reference code_repos.
    memory = await deps.new_memory(store)
    code_graph = await deps.new_code_graph(store)
    conflicts = await deps.new_conflicts(store)
    notes = await deps.new_annotations(store)
    chat = await deps.new_chat(store)
    return Workspace(
        cfg,
        deps,
        store,
        memory,
        code_graph,
        conflicts,
        notes,
        deps.decider(),
        deps.code_embedder(),
        chat,
    )
