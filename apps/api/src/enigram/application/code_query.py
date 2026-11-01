"""Answer a question from the code graph: identifier and route lookup plus full-text search over
flow summaries pick the flows; their summaries and the source of the symbols involved become
numbered sources the answer cites as [n]."""

import logging
import re
import time
from functools import cache
from typing import Any

from enigram.application.code_summary import SOURCE_IS_DATA, SUMMARY_MAX_BODY_CHARS, TRUNCATED
from enigram.application.ports import ChatModel, CodeGraphRepository
from enigram.application.querying import NO_ANSWER_RULE
from enigram.config import Config
from enigram.domain.answer import NO_ANSWER_MARKER, ContextChunk, QueryResult, Source
from enigram.domain.code import IDENTIFIER, CodeSymbol, code_citation

log = logging.getLogger(__name__)

CODE_QUERY_MAX_CONTEXT_TOKENS = 14000  # the same budget as LightRAG's QUERY_MAX_TOTAL_TOKENS
CODE_QUERY_MAX_FLOWS = 3
CODE_QUERY_MAX_TOKENS = 800
CODE_QUERY_SEARCH_LIMIT = 10
CODE_QUERY_REACH_DEPTH = 2  # an entry point counts as related when a hit is this close
CODE_MODE = "code"
NO_SOURCES_ANSWER = "No indexed code matches this question."
# The path may hold ":id" or a dot but never ends in one: "POST /auth/refresh: what…".
_ROUTE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/(?:[\w/:.-]*[\w/])?)", re.IGNORECASE)
ANSWER_PROMPT = (
    SOURCE_IS_DATA + "\n\nAnswer the question about the application from the Context only, in 1-6 "
    "sentences. Every sentence ends with the citation [n] of the source it comes from; a "
    "sentence that uses several sources cites each. Name functions as `Qualified.name`. Prefer "
    "the flow summaries for what happens and the source code for exact values. "
    + NO_ANSWER_RULE
    + "\n\nContext:\n{sources}\n\nQuestion: {question}"
)


@cache
def _encoding() -> Any:
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text, disallowed_special=()))


def _clip(text: str) -> str:
    return (
        text if len(text) <= SUMMARY_MAX_BODY_CHARS else text[:SUMMARY_MAX_BODY_CHARS] + TRUNCATED
    )


async def _reaches(
    graph_repo: CodeGraphRepository, repo_id: int, start: str, targets: set[str], memo: dict
) -> bool:
    frontier, seen = [start], {start}
    for _ in range(CODE_QUERY_REACH_DEPTH):
        nxt = []
        for symbol_id in frontier:
            if symbol_id not in memo:
                memo[symbol_id] = [
                    t.id for _, t in await graph_repo.callees(repo_id, symbol_id) if t
                ]
            for child in memo[symbol_id]:
                if child in targets:
                    return True
                if child not in seen:
                    seen.add(child)
                    nxt.append(child)
        frontier = nxt
    return False


async def answer_code_question(
    cfg: Config,
    chat: ChatModel,
    graph_repo: CodeGraphRepository,
    repo_id: int,
    sha: str,
    question: str,
) -> QueryResult:
    start = time.perf_counter()
    # 1. What the question names: identifiers and routes, then full-text hits.
    hits: list[CodeSymbol] = list(
        await graph_repo.find_symbols(repo_id, IDENTIFIER.findall(question))
    )
    route_entries: list[str] = []
    for verb, path in _ROUTE.findall(question):
        for _, symbol in await graph_repo.routes(repo_id, f"{verb} {path}"):
            route_entries.append(symbol.id)
            hits.append(symbol)
    ranked = await graph_repo.search(repo_id, question, CODE_QUERY_SEARCH_LIMIT)
    by_id = await graph_repo.symbols_by_id(repo_id, [i for i, _ in ranked])
    hits += [by_id[i] for i, _ in ranked if i in by_id]
    order = list(dict.fromkeys(s.id for s in hits))  # first mention wins
    symbols = {s.id: s for s in hits}

    # 2. Flows: entry points among the hits first, then entry points that reach a hit.
    entries = {s.id: s for _, s in await graph_repo.entrypoints(repo_id)}
    flows = [i for i in dict.fromkeys([*route_entries, *order]) if i in entries]
    memo: dict[str, list[str]] = {}
    targets = set(order)
    for entry_id in sorted(entries):
        if len(flows) >= CODE_QUERY_MAX_FLOWS or not targets:
            break
        if entry_id not in flows and await _reaches(graph_repo, repo_id, entry_id, targets, memo):
            flows.append(entry_id)
    flows = flows[:CODE_QUERY_MAX_FLOWS]
    symbols.update({i: entries[i] for i in flows})

    # 3. Sources: flow summaries, then the code of the hits and of the flows' direct callees.
    summaries = await graph_repo.summaries(repo_id, flows)
    candidates: list[tuple[CodeSymbol, str, str]] = []
    for entry_id in flows:
        if flow := summaries.get(f"{entry_id}:flow"):
            candidates.append((entries[entry_id], "flow", flow.text))
    callees: list[CodeSymbol] = []
    for entry_id in flows:
        callees += [t for _, t in await graph_repo.callees(repo_id, entry_id) if t]
    seen_code: set[str] = set()
    for symbol in [*(symbols[i] for i in order), *(entries[i] for i in flows), *callees]:
        if symbol.id not in seen_code:
            seen_code.add(symbol.id)
            candidates.append((symbol, "code", _clip(symbol.body)))

    sources: list[Source] = []
    chunks: list[ContextChunk] = []
    blocks: list[str] = []
    used = 0
    for symbol, section, text in candidates:
        n = len(sources) + 1
        citation = code_citation(symbol, sha)
        block = f"[{n}] {citation} {symbol.qualified_name} ({section})\n<source>\n{text}\n</source>"
        tokens = count_tokens(block)
        if used + tokens > CODE_QUERY_MAX_CONTEXT_TOKENS:
            continue  # a smaller source further down may still fit
        used += tokens
        blocks.append(block)
        sources.append(
            Source(ref=n, title=symbol.qualified_name, section=section, pages="", citation=citation)
        )
        chunks.append(ContextChunk(ref=n, chunk_id=symbol.id, text=text))

    if not sources:
        log.info("[query] code: no sources (%.1fs)", time.perf_counter() - start)
        return QueryResult(
            question,
            NO_SOURCES_ANSWER,
            [],
            [],
            found=False,
            query_secs=round(time.perf_counter() - start, 1),
            mode=CODE_MODE,
            extra={"flows": [], "context_tokens": 0},
        )

    # 4. The answer.
    prompt = ANSWER_PROMPT.format(sources="\n\n".join(blocks), question=question)
    reply = await chat.chat(
        [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=CODE_QUERY_MAX_TOKENS
    )
    found = NO_ANSWER_MARKER not in reply.text
    result = QueryResult(
        question,
        reply.text.replace(NO_ANSWER_MARKER, "").strip(),
        sources,
        chunks,
        found=found,
        query_secs=round(time.perf_counter() - start, 1),
        mode=CODE_MODE,
        tokens_in=reply.tokens_in,
        tokens_out=reply.tokens_out,
        extra={"flows": flows, "context_tokens": used},
    )
    log.info(
        "[query] code: %d sources, %d flows, %d in / %d out tokens (%.1fs)",
        len(sources),
        len(flows),
        reply.tokens_in,
        reply.tokens_out,
        result.query_secs,
    )
    return result
