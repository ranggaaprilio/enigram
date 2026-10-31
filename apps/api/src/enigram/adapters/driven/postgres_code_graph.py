"""CodeGraphRepository on Postgres (asyncpg): the code_* tables, sharing the job store's pool."""

import json
import re
from typing import Any

import asyncpg

from enigram.application.ports import ParsedGraph
from enigram.domain.code import (
    IDENTIFIER,
    IMPLEMENTATION_CALL,
    CodeEdge,
    CodeEntrypoint,
    CodeFile,
    CodeSymbol,
    FlowSummary,
    is_abstract,
)

# CREATE ... IF NOT EXISTS is the whole migration story (as postgres_jobs.SCHEMA). `ref` is ''
# for the default branch: NULL never conflicts in a UNIQUE constraint.
SCHEMA = """
CREATE TABLE IF NOT EXISTS code_repos (
    id BIGSERIAL PRIMARY KEY,
    workspace TEXT NOT NULL,
    url TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT '',
    indexed_sha TEXT NOT NULL,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    report JSONB,
    UNIQUE (workspace, url, ref)
);
CREATE TABLE IF NOT EXISTS code_files (
    repo_id BIGINT NOT NULL REFERENCES code_repos(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    blob_sha TEXT NOT NULL,
    PRIMARY KEY (repo_id, path)
);
CREATE TABLE IF NOT EXISTS code_symbols (
    repo_id BIGINT NOT NULL REFERENCES code_repos(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    file TEXT NOT NULL,
    line_start INT NOT NULL,
    line_end INT NOT NULL,
    body_hash TEXT NOT NULL,
    exported BOOLEAN NOT NULL,
    decorators JSONB NOT NULL DEFAULT '[]',
    signature TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    parent TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (repo_id, id)
);
CREATE INDEX IF NOT EXISTS code_symbols_name ON code_symbols (repo_id, qualified_name);
CREATE TABLE IF NOT EXISTS code_edges (
    repo_id BIGINT NOT NULL REFERENCES code_repos(id) ON DELETE CASCADE,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    kind TEXT NOT NULL,
    src_file TEXT NOT NULL,
    line INT NOT NULL,
    resolved BOOLEAN NOT NULL
);
CREATE INDEX IF NOT EXISTS code_edges_src ON code_edges (repo_id, src, kind);
CREATE INDEX IF NOT EXISTS code_edges_dst ON code_edges (repo_id, dst, kind);
CREATE TABLE IF NOT EXISTS code_entrypoints (
    repo_id BIGINT NOT NULL REFERENCES code_repos(id) ON DELETE CASCADE,
    symbol_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    route TEXT NOT NULL,
    framework TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (repo_id, symbol_id, kind, route)
);
CREATE TABLE IF NOT EXISTS code_summaries (
    repo_id BIGINT NOT NULL REFERENCES code_repos(id) ON DELETE CASCADE,
    symbol_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    text TEXT NOT NULL,
    input_hashes TEXT[] NOT NULL,
    tokens_in INT NOT NULL DEFAULT 0,
    tokens_out INT NOT NULL DEFAULT 0,
    model TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, symbol_id, kind)
);
CREATE INDEX IF NOT EXISTS code_summaries_fts ON code_summaries
    USING GIN (to_tsvector('english', text));
"""

# The question's stemmed words OR-ed: any word may match, ts_rank prefers more matches.
_OR_QUERY = (
    "(SELECT array_to_string(array(SELECT quote_literal(l) "
    "FROM unnest(tsvector_to_array(to_tsvector('english', $2))) l), ' | ')::tsquery AS q) AS words"
)
# "/api/v1/auth/refresh" asks for the route Nest declares as "/auth/refresh".
_ROUTE_PREFIX = re.compile(r"^(/api)?(/v\d+)?(?=/)", re.IGNORECASE)
NAME_MATCH_RANK = 1.0
_REPO_TABLES = ("code_files", "code_symbols", "code_edges", "code_entrypoints")
_SYMBOL_COLUMNS = (
    "id, qualified_name, kind, file, line_start, line_end, body_hash, exported, decorators, "
    "signature, body, parent"
)


def _loads(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def _escape_like(text: str) -> str:
    """For LIKE patterns; backslash is also Postgres' default LIKE escape (LIKE ANY takes no
    ESCAPE clause)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _symbol(row: asyncpg.Record, prefix: str = "") -> CodeSymbol:
    return CodeSymbol(
        id=row[f"{prefix}id"],
        qualified_name=row[f"{prefix}qualified_name"],
        kind=row[f"{prefix}kind"],
        file=row[f"{prefix}file"],
        line_start=row[f"{prefix}line_start"],
        line_end=row[f"{prefix}line_end"],
        body_hash=row[f"{prefix}body_hash"],
        exported=row[f"{prefix}exported"],
        decorators=tuple(_loads(row[f"{prefix}decorators"]) or ()),
        signature=row[f"{prefix}signature"],
        body=row[f"{prefix}body"],
        parent=row[f"{prefix}parent"],
    )


def _entrypoint(row: asyncpg.Record) -> CodeEntrypoint:
    return CodeEntrypoint(
        symbol=row["id"],
        kind=row["e_kind"],
        route=row["route"],
        framework=row["framework"],
        version=row["version"],
    )


def _repo(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "url": row["url"],
        "ref": row["ref"] or None,
        "indexed_sha": row["indexed_sha"],
        "indexed_at": row["indexed_at"],
        "report": _loads(row["report"]),
    }


class PostgresCodeGraph:
    """Repositories are scoped to one workspace; every other table hangs off a repo id that
    get_repo / list_repos returned for that workspace. Shares the job store's pool."""

    def __init__(self, pool: asyncpg.Pool, workspace: str):
        self.pool = pool
        self.workspace = workspace

    async def ensure_schema(self) -> None:
        await self.pool.execute(SCHEMA)

    async def replace_repo(
        self,
        url: str,
        ref: str | None,
        commit_sha: str,
        files: list[CodeFile],
        graph: ParsedGraph,
    ) -> int:
        async with self.pool.acquire() as conn, conn.transaction():
            repo_id = await conn.fetchval(
                "INSERT INTO code_repos (workspace, url, ref, indexed_sha, report) "
                "VALUES ($1, $2, $3, $4, $5::jsonb) ON CONFLICT (workspace, url, ref) DO UPDATE "
                "SET indexed_sha = EXCLUDED.indexed_sha, indexed_at = now(), "
                "report = EXCLUDED.report RETURNING id",
                self.workspace,
                url,
                ref or "",
                commit_sha,
                json.dumps(graph.report),
            )
            for table in _REPO_TABLES:  # summaries stay: build_summaries prunes them
                await conn.execute(f"DELETE FROM {table} WHERE repo_id = $1", repo_id)
            await conn.executemany(
                "INSERT INTO code_files (repo_id, path, blob_sha) VALUES ($1, $2, $3)",
                [(repo_id, f.path, f.blob_sha) for f in files],
            )
            await conn.executemany(
                f"INSERT INTO code_symbols (repo_id, commit_sha, {_SYMBOL_COLUMNS}) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13, $14)",
                [
                    (
                        repo_id,
                        commit_sha,
                        s.id,
                        s.qualified_name,
                        s.kind,
                        s.file,
                        s.line_start,
                        s.line_end,
                        s.body_hash,
                        s.exported,
                        json.dumps(list(s.decorators)),
                        s.signature,
                        s.body,
                        s.parent,
                    )
                    for s in graph.symbols
                ],
            )
            await conn.executemany(
                "INSERT INTO code_edges (repo_id, src, dst, kind, src_file, line, resolved) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                [
                    (repo_id, e.src, e.dst, e.kind, e.src_file, e.line, e.resolved)
                    for e in graph.edges
                ],
            )
            await conn.executemany(
                "INSERT INTO code_entrypoints (repo_id, symbol_id, kind, route, framework, "
                "version) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING",
                [
                    (repo_id, e.symbol, e.kind, e.route, e.framework, e.version)
                    for e in graph.entrypoints
                ],
            )
        return repo_id

    async def set_report(self, repo_id: int, report: dict[str, Any]) -> None:
        await self.pool.execute(
            "UPDATE code_repos SET report = $3::jsonb WHERE workspace = $1 AND id = $2",
            self.workspace,
            repo_id,
            json.dumps(report),
        )

    async def get_repo(self, url: str, ref: str | None) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM code_repos WHERE workspace = $1 AND url = $2 AND ref = $3",
            self.workspace,
            url,
            ref or "",
        )
        return _repo(row) if row else None

    async def list_repos(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM code_repos WHERE workspace = $1 ORDER BY url, ref", self.workspace
        )
        return [_repo(r) for r in rows]

    async def entrypoints(self, repo_id: int) -> list[tuple[CodeEntrypoint, CodeSymbol]]:
        rows = await self.pool.fetch(
            "SELECT e.kind AS e_kind, e.route, e.framework, e.version, s.* "
            "FROM code_entrypoints e JOIN code_symbols s "
            "ON s.repo_id = e.repo_id AND s.id = e.symbol_id "
            "WHERE e.repo_id = $1 ORDER BY s.file, s.line_start, e.route",
            repo_id,
        )
        return [(_entrypoint(r), _symbol(r)) for r in rows]

    async def symbols_by_id(self, repo_id: int, ids: list[str]) -> dict[str, CodeSymbol]:
        rows = await self.pool.fetch(
            f"SELECT {_SYMBOL_COLUMNS} FROM code_symbols "
            "WHERE repo_id = $1 AND id = ANY($2::text[])",
            repo_id,
            list(ids),
        )
        return {r["id"]: _symbol(r) for r in rows}

    async def symbols_in_repo(self, repo_id: int) -> list[CodeSymbol]:
        rows = await self.pool.fetch(
            f"SELECT {_SYMBOL_COLUMNS} FROM code_symbols WHERE repo_id = $1 "
            "ORDER BY file, line_start",
            repo_id,
        )
        return [_symbol(r) for r in rows]

    async def find_symbols(self, repo_id: int, names: list[str]) -> list[CodeSymbol]:
        if not names:
            return []
        suffixes = [f"%.{_escape_like(n)}" for n in names]
        rows = await self.pool.fetch(
            f"SELECT {_SYMBOL_COLUMNS} FROM code_symbols WHERE repo_id = $1 "
            "AND (qualified_name = ANY($2::text[]) "
            "OR qualified_name LIKE ANY($3::text[])) ORDER BY file, line_start",
            repo_id,
            list(names),
            suffixes,
        )
        return [_symbol(r) for r in rows]

    async def callees(
        self, repo_id: int, symbol_id: str
    ) -> list[tuple[CodeEdge, CodeSymbol | None]]:
        rows = await self.pool.fetch(
            "SELECT e.src, e.dst, e.kind AS e_kind, e.src_file, e.line, e.resolved, "
            "s.id AS s_id, s.qualified_name AS s_qualified_name, s.kind AS s_kind, "
            "s.file AS s_file, s.line_start AS s_line_start, s.line_end AS s_line_end, "
            "s.body_hash AS s_body_hash, s.exported AS s_exported, "
            "s.decorators AS s_decorators, s.signature AS s_signature, s.body AS s_body, "
            "s.parent AS s_parent FROM code_edges e "
            "LEFT JOIN code_symbols s ON s.repo_id = e.repo_id AND s.id = e.dst "
            "WHERE e.repo_id = $1 AND e.src = $2 AND e.kind IN ('calls', 'calls_external') "
            "ORDER BY e.line, e.dst",
            repo_id,
            symbol_id,
        )
        out: list[tuple[CodeEdge, CodeSymbol | None]] = []
        for r in rows:
            edge = CodeEdge(
                r["src"], r["dst"], r["e_kind"], r["src_file"], r["line"], r["resolved"]
            )
            target = _symbol(r, "s_") if r["s_id"] else None
            out.append((edge, target))
            if target and target.parent and is_abstract(target):
                for impl in await self._implementations(repo_id, target):
                    out.append(
                        (
                            CodeEdge(
                                edge.src,
                                impl.id,
                                IMPLEMENTATION_CALL,
                                edge.src_file,
                                edge.line,
                                True,
                            ),
                            impl,
                        )
                    )
        return out

    async def _implementations(self, repo_id: int, member: CodeSymbol) -> list[CodeSymbol]:
        """The same member on classes that implement or extend the member's interface/class."""
        owner = f"{member.file}#{member.parent}"
        classes = await self.pool.fetch(
            "SELECT DISTINCT src FROM code_edges WHERE repo_id = $1 AND dst = $2 "
            "AND kind IN ('implements', 'extends') ORDER BY src",
            repo_id,
            owner,
        )
        name = member.qualified_name.rsplit(".", 1)[-1]
        ids = [f"{r['src']}.{name}" for r in classes]
        found = await self.symbols_by_id(repo_id, ids)
        return [found[i] for i in ids if i in found]

    async def search(self, repo_id: int, query: str, limit: int = 10) -> list[tuple[str, float]]:
        ranks: dict[str, float] = {}
        rows = await self.pool.fetch(
            "SELECT symbol_id, max(ts_rank(to_tsvector('english', text), q)) AS rank "
            f"FROM code_summaries, {_OR_QUERY} "
            "WHERE repo_id = $1 AND to_tsvector('english', text) @@ q "
            "GROUP BY symbol_id ORDER BY rank DESC, symbol_id LIMIT $3",
            repo_id,
            query,
            limit,
        )
        for r in rows:
            ranks[r["symbol_id"]] = float(r["rank"])
        names = sorted(set(IDENTIFIER.findall(query)))
        if names:
            rows = await self.pool.fetch(
                "SELECT id FROM code_symbols WHERE repo_id = $1 "
                "AND qualified_name ILIKE ANY($2::text[]) ORDER BY id LIMIT $3",
                repo_id,
                [f"%{_escape_like(n)}%" for n in names],
                limit,
            )
            for r in rows:
                ranks[r["id"]] = max(ranks.get(r["id"], 0.0), NAME_MATCH_RANK)
        return sorted(ranks.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    async def save_summaries(
        self, repo_id: int, commit_sha: str, summaries: list[FlowSummary], model: str
    ) -> None:
        await self.pool.executemany(
            "INSERT INTO code_summaries (repo_id, symbol_id, kind, commit_sha, text, "
            "input_hashes, tokens_in, tokens_out, model) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
            "ON CONFLICT (repo_id, symbol_id, kind) DO UPDATE SET "
            "commit_sha = EXCLUDED.commit_sha, text = EXCLUDED.text, "
            "input_hashes = EXCLUDED.input_hashes, tokens_in = EXCLUDED.tokens_in, "
            "tokens_out = EXCLUDED.tokens_out, model = EXCLUDED.model, created_at = now()",
            [
                (
                    repo_id,
                    s.symbol,
                    s.kind,
                    commit_sha,
                    s.text,
                    list(s.input_hashes),
                    s.tokens_in,
                    s.tokens_out,
                    model,
                )
                for s in summaries
            ],
        )

    async def summaries(self, repo_id: int, symbol_ids: list[str]) -> dict[str, FlowSummary]:
        rows = await self.pool.fetch(
            "SELECT symbol_id, kind, text, input_hashes, tokens_in, tokens_out "
            "FROM code_summaries WHERE repo_id = $1 AND symbol_id = ANY($2::text[])",
            repo_id,
            list(symbol_ids),
        )
        return {
            f"{r['symbol_id']}:{r['kind']}": FlowSummary(
                symbol=r["symbol_id"],
                kind=r["kind"],
                text=r["text"],
                input_hashes=tuple(r["input_hashes"]),
                tokens_in=r["tokens_in"],
                tokens_out=r["tokens_out"],
            )
            for r in rows
        }

    async def delete_summaries_except(self, repo_id: int, keep: list[tuple[str, str]]) -> int:
        status = await self.pool.execute(
            "DELETE FROM code_summaries WHERE repo_id = $1 "
            "AND NOT (symbol_id || ':' || kind = ANY($2::text[]))",
            repo_id,
            [f"{symbol}:{kind}" for symbol, kind in keep],
        )
        return int(status.split()[-1])

    async def routes(self, repo_id: int, verb_path: str) -> list[tuple[CodeEntrypoint, CodeSymbol]]:
        verb, _, path = verb_path.strip().partition(" ")
        path = _ROUTE_PREFIX.sub("", path.strip()).rstrip("/") or "/"
        rows = await self.pool.fetch(
            "SELECT e.kind AS e_kind, e.route, e.framework, e.version, s.* "
            "FROM code_entrypoints e JOIN code_symbols s "
            "ON s.repo_id = e.repo_id AND s.id = e.symbol_id "
            "WHERE e.repo_id = $1 AND e.kind = 'http_route' AND e.route ILIKE $2 ESCAPE '\\' "
            "ORDER BY e.route",
            repo_id,
            f"{_escape_like(verb.upper())} {_escape_like(path)}",
        )
        return [(_entrypoint(r), _symbol(r)) for r in rows]
