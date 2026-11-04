"""Code embeddings (CODE_EMBED_MODEL) for the semantic part of search_code: every flow/symbol
summary and every symbol's own text, embedded once per text hash; and the rank fusion that
merges them with the full-text ranks."""

import logging
import time
from hashlib import sha256
from typing import Any

from enigram.application.ports import CodeEmbedder, CodeGraphRepository
from enigram.domain.code import NAME_MATCH_RANK, CodeSymbol

log = logging.getLogger(__name__)

EMBED_BODY_CHARS = 2000  # head of a symbol body; signature and decorators carry the rest
RRF_K = 60  # reciprocal rank fusion constant (Cormack et al. 2009)
SAVE_BATCH = 128  # one Voyage batch per save


def _hash(text: str) -> str:
    return sha256(text.encode()).hexdigest()[:16]


def symbol_text(s: CodeSymbol) -> str:
    head = [f"{s.qualified_name} ({s.kind}) {s.file}", s.signature, " ".join(s.decorators_text)]
    return "\n".join(p for p in head if p) + "\n" + s.body[:EMBED_BODY_CHARS]


def fuse_ranks(*rankings: list[tuple[str, float]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Reciprocal rank fusion of ranked (id, score) lists; best first, ties by id."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, (item, _) in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def rank_search(
    text: list[tuple[str, float]], near: list[tuple[str, float]], limit: int
) -> list[tuple[str, float]]:
    """search_code's ranking of one repository with the embeddings: the name matches first (as
    without them), then the fusion of the full-text and vector ranks. Scored 1/position, so the
    hits of repositories with and without embeddings compare by position."""
    pinned = [i for i, rank in text if rank >= NAME_MATCH_RANK]
    fused = [i for i, _ in fuse_ranks(text, near) if i not in pinned]
    return [(i, 1.0 / p) for p, i in enumerate([*pinned, *fused][:limit], 1)]


async def embed_repo(
    embedder: CodeEmbedder,
    graph_repo: CodeGraphRepository,
    repo_id: int,
    *,
    summarised_only: bool = False,
) -> dict[str, Any]:
    """Embed every text whose hash changed since the last run and drop the rows of symbols and
    summaries that no longer exist. `summarised_only` (a `--limit` trial) embeds only the
    summaries and the code of the symbols that have one, and prunes nothing. Errors propagate:
    the job fails, a rerun reuses every vector saved so far."""
    start = time.perf_counter()
    symbols = await graph_repo.symbols_in_repo(repo_id)
    texts: dict[tuple[str, str], str] = {}
    for key, summary in (await graph_repo.summaries(repo_id, [s.id for s in symbols])).items():
        # Symbol ids hold "#", "." and "/" but no ":", so the last ":" separates the kind.
        symbol_id, kind = key.rsplit(":", 1)
        if summary.text.strip():
            texts[(symbol_id, kind)] = summary.text
    summarised = {symbol_id for symbol_id, _ in texts}
    for s in symbols:
        if not summarised_only or s.id in summarised:
            texts[(s.id, "code")] = symbol_text(s)
    have = await graph_repo.embedding_hashes(repo_id, embedder.model)
    todo = [(k, t, h) for k, t in sorted(texts.items()) if have.get(k) != (h := _hash(t))]
    for i in range(0, len(todo), SAVE_BATCH):
        batch = todo[i : i + SAVE_BATCH]
        vectors = await embedder.embed([t for _, t, _ in batch], "document")
        rows = [(k[0], k[1], h, v) for (k, _, h), v in zip(batch, vectors, strict=True)]
        await graph_repo.save_embeddings(repo_id, embedder.model, rows)
    pruned = (
        0
        if summarised_only
        else await graph_repo.delete_embeddings_except(repo_id, list(texts), embedder.model)
    )
    result = {
        "model": embedder.model,
        "texts": len(texts),
        "embedded": len(todo),
        "cached": len(texts) - len(todo),
        "pruned": pruned,
        "secs": round(time.perf_counter() - start, 1),
    }
    log.info("[embed] %d embedded, %d cached, %d pruned", len(todo), result["cached"], pruned)
    return result
