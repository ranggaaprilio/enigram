"""CodeGraphRepository on Postgres (asyncpg): the code_* tables, sharing the job store's pool."""

import json
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import asyncpg

from enigram.application.ports import ParsedGraph
from enigram.domain.code import (
    IDENTIFIER,
    IMPLEMENTATION_CALL,
    NAME_MATCH_RANK,
    CodeEdge,
    CodeEntrypoint,
    CodeFile,
    CodeSymbol,
    FlowSummary,
    is_abstract,
)
from enigram.domain.errors import EnigramError

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
ALTER TABLE code_symbols ADD COLUMN IF NOT EXISTS decorators_text JSONB NOT NULL DEFAULT '[]';
CREATE INDEX IF NOT EXISTS code_symbols_fts ON code_symbols
    USING GIN (to_tsvector('english', body));
ALTER TABLE code_repos ADD COLUMN IF NOT EXISTS webhook_secret TEXT;
CREATE TABLE IF NOT EXISTS code_symbol_changes (
    repo_id BIGINT NOT NULL REFERENCES code_repos(id) ON DELETE CASCADE,
    symbol_id TEXT NOT NULL,
    old_hash TEXT,
    new_hash TEXT,
    sha TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS code_symbol_changes_repo ON code_symbol_changes (repo_id, at);
-- every index of a repository, after its symbol changes (what_changed_since resolves a SHA here)
CREATE TABLE IF NOT EXISTS code_index_runs (
    repo_id BIGINT NOT NULL REFERENCES code_repos(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS code_index_runs_repo ON code_index_runs (repo_id, sha);
"""


def embeddings_schema(dim: int) -> str:
    # The dimension is fixed by CODE_EMBED_MODEL; another CODE_EMBED_DIM needs this table dropped.
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS code_embeddings (
    repo_id BIGINT NOT NULL REFERENCES code_repos(id) ON DELETE CASCADE,
    symbol_id TEXT NOT NULL,
    -- "flow" | "symbol" (a summary of that kind) or "code" (the symbol's own text)
    source TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    embedding vector({int(dim)}) NOT NULL,
    PRIMARY KEY (repo_id, symbol_id, source)
);
"""


# The question's stemmed words OR-ed: any word may match, ts_rank prefers more matches.
_OR_QUERY = (
    "(SELECT array_to_string(array(SELECT quote_literal(l) "
    "FROM unnest(tsvector_to_array(to_tsvector('english', $2))) l), ' | ')::tsquery AS q) AS words"
)
# "/api/v1/auth/refresh" asks for the route Nest declares as "/auth/refresh".
_ROUTE_PREFIX = re.compile(r"^(/api)?(/v\d+)?(?=/)", re.IGNORECASE)
# A body hit ranks below a summary hit of the same words: bodies are noisier than summaries.
BODY_MATCH_WEIGHT = 0.5
_REPO_TABLES = ("code_files", "code_symbols", "code_edges", "code_entrypoints")
_SYMBOL_COLUMNS = (
    "id, qualified_name, kind, file, line_start, line_end, body_hash, exported, decorators, "
    "signature, body, parent, decorators_text"
)
# The joined symbol's columns as s_<column>, for _symbol(row, "s_").
_JOINED_SYMBOL_COLUMNS = ", ".join(f"s.{c} AS s_{c}" for c in _SYMBOL_COLUMNS.split(", "))
_EDGE_COLUMNS = "e.src, e.dst, e.kind AS e_kind, e.src_file, e.line, e.resolved"


def _vector(values: Sequence[float]) -> str:
    """pgvector's text form, cast with ::vector, so no Python codec is needed."""
    return "[" + ",".join(f"{float(v):.7g}" for v in values) + "]"


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
        decorators_text=tuple(_loads(row[f"{prefix}decorators_text"]) or ()),
    )


def _edge(row: asyncpg.Record) -> CodeEdge:
    return CodeEdge(
        row["src"], row["dst"], row["e_kind"], row["src_file"], row["line"], row["resolved"]
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
        # The secret itself only through webhook_secret().
        "webhook": row["webhook_secret"] is not None,
    }


class PostgresCodeGraph:
    """Repositories are scoped to one workspace; every other table hangs off a repo id that
    get_repo / list_repos returned for that workspace. Shares the job store's pool."""

    def __init__(self, pool: asyncpg.Pool, workspace: str, embed_dim: int = 1024):
        self.pool = pool
        self.workspace = workspace
        self.embed_dim = embed_dim

    async def ensure_schema(self) -> None:
        await self.pool.execute(SCHEMA)
        await self.pool.execute(embeddings_schema(self.embed_dim))
        # CREATE TABLE IF NOT EXISTS keeps an older dimension; pgvector stores it as the typmod.
        stored = await self.pool.fetchval(
            "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'code_embeddings'::regclass "
            "AND attname = 'embedding'"
        )
        if stored is not None and stored != self.embed_dim:
            raise EnigramError(
                f"code_embeddings holds vector({stored}) but CODE_EMBED_DIM={self.embed_dim}; "
                "drop the table (DROP TABLE code_embeddings) and re-index, or set CODE_EMBED_DIM "
                "back"
            )

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
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13, $14, "
                "$15::jsonb)",
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
                        json.dumps(list(s.decorators_text)),
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
            f"SELECT {_EDGE_COLUMNS}, {_JOINED_SYMBOL_COLUMNS} FROM code_edges e "
            "LEFT JOIN code_symbols s ON s.repo_id = e.repo_id AND s.id = e.dst "
            "WHERE e.repo_id = $1 AND e.src = $2 AND e.kind IN ('calls', 'calls_external') "
            "ORDER BY e.line, e.dst",
            repo_id,
            symbol_id,
        )
        out: list[tuple[CodeEdge, CodeSymbol | None]] = []
        for r in rows:
            edge = _edge(r)
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

    async def edges_from(
        self, repo_id: int, symbol_id: str, kinds: tuple[str, ...]
    ) -> list[tuple[CodeEdge, CodeSymbol | None]]:
        rows = await self.pool.fetch(
            f"SELECT {_EDGE_COLUMNS}, {_JOINED_SYMBOL_COLUMNS} FROM code_edges e "
            "LEFT JOIN code_symbols s ON s.repo_id = e.repo_id AND s.id = e.dst "
            "WHERE e.repo_id = $1 AND e.src = $2 AND e.kind = ANY($3::text[]) "
            "ORDER BY e.kind, e.line, e.dst",
            repo_id,
            symbol_id,
            list(kinds),
        )
        return [(_edge(r), _symbol(r, "s_") if r["s_id"] else None) for r in rows]

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

    async def search(
        self, repo_id: int, query: str, limit: int = 10, *, bodies: bool = False
    ) -> list[tuple[str, float]]:
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
        if bodies:
            # Modules and config factories have no summary (no entry point reaches them): their
            # bodies are the only text that names `fileSize` or `registerAs`.
            rows = await self.pool.fetch(
                "SELECT id, ts_rank(to_tsvector('english', body), q) * $4 AS rank "
                f"FROM code_symbols, {_OR_QUERY} "
                "WHERE repo_id = $1 AND to_tsvector('english', body) @@ q "
                "ORDER BY rank DESC, id LIMIT $3",
                repo_id,
                query,
                limit,
                BODY_MATCH_WEIGHT,
            )
            for r in rows:
                ranks[r["id"]] = max(ranks.get(r["id"], 0.0), float(r["rank"]))
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

    async def embedding_hashes(self, repo_id: int, model: str) -> dict[tuple[str, str], str]:
        rows = await self.pool.fetch(
            "SELECT symbol_id, source, text_hash FROM code_embeddings "
            "WHERE repo_id = $1 AND model = $2",
            repo_id,
            model,
        )
        return {(r["symbol_id"], r["source"]): r["text_hash"] for r in rows}

    async def save_embeddings(
        self, repo_id: int, model: str, rows: list[tuple[str, str, str, list[float]]]
    ) -> None:
        await self.pool.executemany(
            "INSERT INTO code_embeddings (repo_id, symbol_id, source, text_hash, model, embedding) "
            "VALUES ($1, $2, $3, $4, $5, $6::vector) "
            "ON CONFLICT (repo_id, symbol_id, source) DO UPDATE SET "
            "text_hash = EXCLUDED.text_hash, model = EXCLUDED.model, "
            "embedding = EXCLUDED.embedding",
            [(repo_id, symbol, source, h, model, _vector(v)) for symbol, source, h, v in rows],
        )

    async def delete_embeddings_except(
        self, repo_id: int, keep: list[tuple[str, str]], model: str
    ) -> int:
        status = await self.pool.execute(
            "DELETE FROM code_embeddings WHERE repo_id = $1 "
            "AND (model <> $3 OR NOT (symbol_id || ':' || source = ANY($2::text[])))",
            repo_id,
            [f"{symbol}:{source}" for symbol, source in keep],
            model,
        )
        return int(status.split()[-1])

    async def search_vectors(
        self, repo_id: int, vector: list[float], model: str, limit: int
    ) -> list[tuple[str, float]]:
        # Exact scan (no HNSW): a repository holds a few thousand rows at most.
        rows = await self.pool.fetch(
            "SELECT symbol_id, max(1 - (embedding <=> $2::vector)) AS sim FROM code_embeddings "
            "WHERE repo_id = $1 AND model = $3 GROUP BY symbol_id "
            "ORDER BY sim DESC, symbol_id LIMIT $4",
            repo_id,
            _vector(vector),
            model,
            limit,
        )
        return [(r["symbol_id"], float(r["sim"])) for r in rows]

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

    async def get_repo_by_id(self, repo_id: int) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM code_repos WHERE workspace = $1 AND id = $2", self.workspace, repo_id
        )
        return _repo(row) if row else None

    async def webhook_secret(self, repo_id: int) -> str | None:
        return await self.pool.fetchval(
            "SELECT webhook_secret FROM code_repos WHERE workspace = $1 AND id = $2",
            self.workspace,
            repo_id,
        )

    async def set_webhook_secret(self, repo_id: int, secret: str) -> None:
        await self.pool.execute(
            "UPDATE code_repos SET webhook_secret = $3 WHERE workspace = $1 AND id = $2",
            self.workspace,
            repo_id,
            secret,
        )

    async def delete_repo(self, repo_id: int) -> None:
        await self.pool.execute(
            "DELETE FROM code_repos WHERE workspace = $1 AND id = $2", self.workspace, repo_id
        )

    async def symbol_hashes(self, repo_id: int) -> dict[str, str]:
        rows = await self.pool.fetch(
            "SELECT id, body_hash FROM code_symbols WHERE repo_id = $1", repo_id
        )
        return {r["id"]: r["body_hash"] for r in rows}

    async def files(self, repo_id: int) -> list[CodeFile]:
        rows = await self.pool.fetch(
            "SELECT path, blob_sha FROM code_files WHERE repo_id = $1 ORDER BY path", repo_id
        )
        return [CodeFile(r["path"], r["blob_sha"]) for r in rows]

    async def import_edges(self, repo_id: int) -> list[tuple[str, str]]:
        # src and dst of an `imports` edge are file paths, not symbol ids.
        rows = await self.pool.fetch(
            "SELECT DISTINCT src, dst FROM code_edges "
            "WHERE repo_id = $1 AND kind = 'imports' AND resolved ORDER BY src, dst",
            repo_id,
        )
        return [(r["src"], r["dst"]) for r in rows]

    async def record_symbol_changes(
        self, repo_id: int, sha: str, changes: list[tuple[str, str | None, str | None]]
    ) -> None:
        await self.pool.executemany(
            "INSERT INTO code_symbol_changes (repo_id, symbol_id, old_hash, new_hash, sha) "
            "VALUES ($1, $2, $3, $4, $5)",
            [(repo_id, symbol_id, old, new, sha) for symbol_id, old, new in changes],
        )

    async def record_index(self, repo_id: int, sha: str) -> None:
        await self.pool.execute(
            "INSERT INTO code_index_runs (repo_id, sha) VALUES ($1, $2)", repo_id, sha
        )

    async def indexed_at(self, repo_id: int, sha: str) -> datetime | None:
        # code_symbol_changes too: repositories indexed before code_index_runs existed
        prefix = sha + "%"  # hex only (changes_feed.parse_since): nothing to escape
        return await self.pool.fetchval(
            "SELECT max(at) FROM (SELECT at FROM code_index_runs WHERE repo_id = $1 "
            "AND sha LIKE $2 UNION ALL SELECT at FROM code_symbol_changes WHERE repo_id = $1 "
            "AND sha LIKE $2) runs",
            repo_id,
            prefix,
        )

    async def symbol_changes(
        self, repo_id: int, since: datetime | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Recorded changes after `since`, oldest first (what_changed_since)."""
        rows = await self.pool.fetch(
            "SELECT symbol_id, old_hash, new_hash, sha, at FROM code_symbol_changes "
            "WHERE repo_id = $1 AND ($2::timestamptz IS NULL OR at > $2) "
            "ORDER BY at, symbol_id LIMIT $3",
            repo_id,
            since,
            limit,
        )
        return [dict(r) for r in rows]

    async def symbols_by_hash(self, repo_id: int, body_hashes: list[str]) -> list[CodeSymbol]:
        rows = await self.pool.fetch(
            f"SELECT {_SYMBOL_COLUMNS} FROM code_symbols "
            "WHERE repo_id = $1 AND body_hash = ANY($2::text[]) ORDER BY file, line_start",
            repo_id,
            list(body_hashes),
        )
        return [_symbol(r) for r in rows]
