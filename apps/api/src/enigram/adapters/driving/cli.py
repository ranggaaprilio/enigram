"""Index written documents into a LightRAG knowledge graph."""

import argparse
import asyncio
import dataclasses
import logging
import sys
from pathlib import Path

from enigram.config import Config
from enigram.domain.errors import EnigramError


def _page_range(value: str) -> tuple[int, int]:
    try:
        start, end = (int(x) for x in value.split("-", 1))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected A-B, got {value!r}") from e
    if start < 1 or end < start:
        raise argparse.ArgumentTypeError(f"invalid page range {value!r}")
    return start, end


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="enigram", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "check", help="verify the DeepSeek and Voyage endpoints, thinking mode and embedding dim"
    )

    index = sub.add_parser("index", help="parse PDF/Markdown files and build the LightRAG graph")
    index.add_argument("folder", type=Path, help="folder containing *.pdf, *.md or *.markdown")
    index.add_argument(
        "--pages",
        type=_page_range,
        help="only PDF pages A-B (Markdown is read whole), for quick trials; "
        "writes to <DATA_DIR>/dev to keep the real index",
    )
    index.add_argument("--only", nargs="+", metavar="STEM", help="only these books (file stems)")

    report = sub.add_parser("report", help="rebuild the index quality report")
    report.add_argument("--review", type=Path, help="scored review checklist to include")

    query = sub.add_parser("query", help="answer a question from the index, with sources")
    query.add_argument("question", help="the question, quoted")
    # Literal copy of query.QUERY_MODES so parsing never imports LightRAG; a test keeps them equal.
    query.add_argument(
        "--mode",
        choices=("mix", "naive"),
        default="mix",
        help="mix = GraphRAG (graph + vector, default); naive = vector-only baseline",
    )
    query.add_argument(
        "--scope",
        choices=("docs", "code"),
        default="docs",
        help="docs = the LightRAG index (default); code = the graph-first agent over the "
        "indexed TypeScript repositories (ignores --mode)",
    )
    query.add_argument("--repo", help="repository URL when more than one is indexed")

    repo = sub.add_parser(
        "repo", help="index one repository (Markdown and TypeScript code graph) without the API"
    )
    repo.add_argument("url", help="https URL of a public repository")
    repo.add_argument("--ref", help="branch or tag (default: the default branch)")
    repo.add_argument(
        "--limit",
        type=int,
        help="summarise only the first N entry points (quick trial)",
    )

    sub.add_parser("mcp", help="run the MCP server on stdio (for Claude Code)")

    api = sub.add_parser("api", help="run the HTTP API (upload, query, query traces)")
    api.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 in a container)")
    api.add_argument("--port", type=int, default=8080)

    sub.add_parser("worker", help="index uploaded documents from the job queue")

    golden = sub.add_parser("golden", help="golden set: check questions, answer them, count labels")
    gsub = golden.add_subparsers(dest="golden_command", required=True)
    check = gsub.add_parser(
        "check",
        help="validate the golden set against the index (document set) or the pinned "
        "repository (code set)",
    )
    check.add_argument(
        "--graph",
        action="store_true",
        help="check evidence symbols by qualified name in the indexed code graph (needs "
        "`enigram repo` on the pinned ref)",
    )
    answer = gsub.add_parser("answer", help="answer golden questions and write a label sheet")
    for p in (check, answer):
        p.add_argument("--file", type=Path, default=Path("eval/golden.yaml"), help="golden YAML")
    # Literal copy of golden_run.SYSTEMS so parsing never imports LightRAG; a test keeps them equal.
    answer.add_argument(
        "--system",
        choices=("mix", "naive", "code", "agent+repo", "enigram-agent"),
        default="mix",
        help="mix/naive = the document index; code = the code graph (one prompt, frozen "
        "ablation); agent+repo = a coding agent reading the pinned clone; enigram-agent = the "
        "graph-first agent over the repository store",
    )
    answer.add_argument("--split", choices=("dev", "verdict", "reserve"), help="only this split")
    answer.add_argument(
        "--category", action="append", help="only this category (repeat for several)"
    )
    answer.add_argument(
        "--allow-verdict",
        action="store_true",
        help="allow the verdict and reserve splits (locked until Phase 7)",
    )
    answer.add_argument("--limit", type=int, help="only the first N questions (quick trial)")
    citations = gsub.add_parser(
        "citations", help="check every file:line@sha citation of an answers file"
    )
    citations.add_argument("answers", type=Path, help="data/eval/answers-<system>-<ts>.json")
    labels = gsub.add_parser("labels", help="count human claim labels in a label sheet")
    labels.add_argument("sheet", type=Path, help="data/eval/labels-<mode>-<ts>.md")
    blind = gsub.add_parser(
        "blind", help="one blind label sheet from several systems' answers (normalized, shuffled)"
    )
    blind.add_argument(
        "answers",
        type=Path,
        nargs="+",
        metavar="ANSWERS",
        help="data/eval/answers-<mode>-<ts>.json, one per system",
    )
    blind.add_argument("--seed", type=int, help="shuffle seed (default random; kept in the key)")
    unblind = gsub.add_parser("unblind", help="score a labelled blind sheet per system")
    unblind.add_argument("sheet", type=Path, help="data/eval/blind-<ts>.md")

    evaluate = sub.add_parser(
        "eval",
        help="score answer files against blind labels (with --judge: Ragas + LLM claim judge)",
    )
    evaluate.add_argument(
        "--answers",
        type=Path,
        nargs="+",
        required=True,
        metavar="ANSWERS",
        help="data/eval/answers-<system>-<ts>.json, one per system",
    )
    evaluate.add_argument(
        "--blind",
        type=Path,
        nargs="*",
        default=[],
        metavar="SHEET",
        help="labelled blind sheets (data/eval/blind-<ts>.md) with their key files",
    )
    evaluate.add_argument(
        "--labels",
        type=Path,
        nargs="*",
        default=[],
        metavar="SHEET",
        help="human claim label sheets (labels-<system>-<ts>.md) to measure the judge against",
    )
    evaluate.add_argument(
        "--judge", action="store_true", help="also run Ragas and the LLM claim judge (slow)"
    )
    return parser


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if level != "DEBUG":
        logging.getLogger("lightrag").setLevel(logging.WARNING)
        logging.getLogger("docling").setLevel(logging.WARNING)
        logging.getLogger("mcp").setLevel(logging.WARNING)  # one INFO line per HTTP request


async def _check(cfg: Config) -> int:
    from enigram.bootstrap import build_deps

    result = await build_deps(cfg).check_llm()
    print(f"✓ {cfg.llm_provider} models served: {', '.join(result.models)}")
    print(f"✓ completion {result.completion_secs:.1f}s (thinking_mode={cfg.thinking_mode})")
    print("✓ JSON mode works" if result.json_mode_ok else "✗ JSON mode failed (see log)")
    print(f"✓ voyage {result.embed_model} dim {result.embed_dim}")
    return 0 if result.json_mode_ok else 1


async def _preflight(cfg: Config) -> None:
    """Open the index once: raises `No index found` before the MCP server starts."""
    from enigram.bootstrap import build_deps

    async with build_deps(cfg).open_engine():
        pass


async def _index(cfg: Config, args: argparse.Namespace) -> int:
    from enigram.application.indexing import run_index
    from enigram.bootstrap import build_deps

    run = await run_index(cfg, build_deps(cfg), args.folder, pages=args.pages, only=args.only)
    for book in run.up_to_date:
        print(f"{book}: up to date (skipped)")
    for book, entry in run.books.items():
        error = f" — {entry['error']}" if entry.get("error") else ""
        print(f"{book}: {entry.get('status')}{error}")
    if run.report_path:
        print(f"report: {run.report_path}")
    return 1 if run.any_failed else 0


async def _query_code(cfg: Config, question: str, url: str | None) -> int:
    from enigram.application.code_agent import answer_with_agent
    from enigram.bootstrap import build_deps

    deps = build_deps(cfg)
    store = await deps.open_store()
    try:
        graph = await deps.new_code_graph(store)
        result = await answer_with_agent(
            cfg, deps.generator(), graph, question, repo_url=url, model=cfg.llm_model
        )
    finally:
        await store.close()
    print(result.answer)
    if result.sources:
        print("\nSources:")
        for s in result.sources:
            print(f"  [{s.ref}] {s.citation} ({s.title})")
    print(
        f"({result.mode}, {result.query_secs:.1f}s, "
        f"{result.tokens_in} in / {result.tokens_out} out tokens, "
        f"{result.extra.get('tool_calls', 0)} tool calls)"
    )
    return 0 if result.found else 1


async def _query(cfg: Config, args: argparse.Namespace) -> int:
    if args.scope == "code":
        return await _query_code(cfg, args.question, args.repo)
    from enigram.application.querying import answer_question
    from enigram.bootstrap import build_deps

    async with build_deps(cfg).open_engine() as engine:
        result = await answer_question(engine, args.question, args.mode)
    print(result.answer)
    if result.sources:
        print("\nSources:")
        for s in result.sources:
            print(f"  [{s.ref}] {s.citation.rsplit(' | #', 1)[0]}")
    print(f"({result.mode}, {result.query_secs:.1f}s)")
    return 0 if result.found else 1


async def _repo(cfg: Config, args: argparse.Namespace) -> int:
    import json
    from datetime import datetime

    from enigram.application.files import atomic_write_text
    from enigram.application.repo_sync import index_repo
    from enigram.bootstrap import build_deps

    deps = build_deps(cfg)
    store = await deps.open_store()
    try:
        graph = await deps.new_code_graph(store)
        notes = await deps.new_annotations(store)
        result = await index_repo(
            cfg, deps, args.url, args.ref, graph=graph, limit=args.limit, notes=notes
        )
    finally:
        await store.close()
    for key, value in result["code"].items():
        if key != "diagnostics_sample":
            print(f"code.{key}: {json.dumps(value, default=str)}")
    print(
        f"markdown: {result['files']} files, {len(result['indexed'])} indexed, "
        f"{len(result['failed'])} failed, {result['unchanged']} unchanged"
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = cfg.reports_dir / f"repo-index-{stamp}.json"
    atomic_write_text(path, json.dumps(result, indent=2, default=str))
    print(f"result: {path}")
    return 1 if result["failed"] else 0


async def _graph_symbols(cfg: Config, url: str, ref: str | None, pinned_sha: str) -> list:
    """Symbols of the indexed repository at the pinned commit."""
    from enigram.bootstrap import build_deps

    deps = build_deps(cfg)
    store = await deps.open_store()
    try:
        graph = await deps.new_code_graph(store)
        repo = await graph.get_repo(url, ref)
        run = f"`enigram repo {url}" + (f" --ref {ref}`" if ref else "`")
        if repo is None:
            raise EnigramError(f"{url} is not indexed; run {run}")
        if repo["indexed_sha"] != pinned_sha:
            raise EnigramError(
                f"{url} is indexed at {repo['indexed_sha'][:12]}, the golden set pins "
                f"{pinned_sha[:12]}; run {run}"
            )
        return await graph.symbols_in_repo(repo["id"])
    finally:
        await store.close()


def _golden_check(cfg: Config, path: Path, graph: bool = False) -> int:
    from enigram.application.evaluation.golden import (
        BEHAVIOR,
        BEHAVIOR_ANNOTATION,
        DOC_CATEGORIES,
        INJECTION,
        SPLITS,
        TARGET_BEHAVIOR_ANNOTATION,
        TARGET_INJECTION,
        TARGET_PER_CATEGORY,
        TARGET_SPLITS,
        TARGET_UNANSWERABLE,
        TARGET_UNANSWERABLE_CODE,
        UNANSWERABLE,
        check_golden,
        load_golden_set,
    )
    from enigram.application.files import file_sha256

    symbols = None
    if graph:
        repo = load_golden_set(path).repo
        if repo is None:
            raise EnigramError("--graph checks a code golden set (one with `repo:`)")
        symbols = asyncio.run(_graph_symbols(cfg, repo.url, repo.ref, repo.pinned_sha))
    golden_set, problems = check_golden(cfg, path, symbols=symbols)
    questions = golden_set.questions
    width = 14 if golden_set.repo is None else 20
    for q in questions:
        head = f"{q.id:<6} {q.category:<{width}}"
        if golden_set.repo is not None:
            head += f" {q.split or '-':<8}"
        if q.id in problems:
            print(f"{head} ✗")
            for problem in problems[q.id]:
                print(f"         {problem}")
        else:
            print(f"{head} ok  {' '.join(ev.file or ev.chunk_id for ev in q.evidence)}")
    if not questions:
        print(f"{path}: no questions yet")
    if golden_set.repo is not None:
        repo = golden_set.repo
        counts = [
            f"{BEHAVIOR} {s} "
            f"{sum(q.category == BEHAVIOR and q.split == s for q in questions)}/{TARGET_SPLITS[s]}"
            for s in SPLITS
        ]
        for cat, target in (
            (BEHAVIOR_ANNOTATION, TARGET_BEHAVIOR_ANNOTATION),
            (UNANSWERABLE, TARGET_UNANSWERABLE_CODE),
            (INJECTION, TARGET_INJECTION),
        ):
            counts.append(f"{cat} {sum(q.category == cat for q in questions)}/≥{target}")
        print(" · ".join(counts))
        print(f"repo {repo.url} @ {repo.ref} = {repo.pinned_sha[:12]}")
        print(f"golden sha256 {file_sha256(path)}")
        return 1 if problems or not questions else 0
    papers = {ev.book for q in questions for ev in q.evidence if ev.book}
    counts = []
    for cat in DOC_CATEGORIES:
        n = sum(q.category == cat for q in questions)
        note = " (needs ≥2 indexed papers)" if cat == "multi_paper" and len(papers) < 2 else ""
        target = TARGET_UNANSWERABLE if cat == UNANSWERABLE else TARGET_PER_CATEGORY
        counts.append(f"{cat} {n}/{target}{note}")
    print(" · ".join(counts))
    return 1 if problems or not questions else 0


async def _golden_answer(cfg: Config, args: argparse.Namespace) -> int:
    from enigram.application.evaluation.golden_run import run_golden
    from enigram.bootstrap import build_deps

    run = await run_golden(
        cfg,
        build_deps(cfg),
        args.file,
        args.system,
        args.limit,
        split=args.split,
        categories=tuple(args.category) if args.category else None,
        allow_verdict=args.allow_verdict,
    )
    print(f"answers: {run.answers_path}")
    print(f"labels:  {run.labels_path}")
    if run.failed:
        print(f"{run.failed} question(s) failed; see the answers file")
    return 1 if run.failed else 0


async def _golden_citations(cfg: Config, path: Path) -> int:
    from enigram.application.evaluation.blind import load_answers
    from enigram.application.evaluation.citations import check_answers, citations_path

    run = load_answers(path)
    repo = run.get("repo")
    symbols = None
    if repo and cfg.storage == "postgres":
        try:
            symbols = await _graph_symbols(cfg, repo["url"], repo["ref"], repo["pinned_sha"])
        except EnigramError as e:
            print(f"note: symbols not checked ({e})")
    result = check_answers(cfg, path, symbols)
    rows = []
    for qid, item in result["items"].items():
        valid = sum(c["valid"] for c in item["citations"])
        rows.append(f"{qid} {valid}/{len(item['citations'])} valid")
        for c in item["citations"]:
            if not c["valid"]:
                print(f"  {qid}: {c['citation']}: {c['reason']}")
    print(" · ".join(rows))
    print(
        f"{result['answers_fully_valid']}/{result['answers']} answers fully valid · "
        f"{result['citations_valid']}/{result['citations']} citations valid · "
        f"{result['sentences_without_citation']} sentences without citation"
    )
    print(f"result: {citations_path(path)}")
    return 0


def _golden_labels(sheet: Path) -> int:
    from enigram.application.evaluation.claims import count_claims, label_summary, read_labels

    s = label_summary(read_labels(sheet), count_claims(sheet))
    target = "✓" if s["labelled"] >= 30 else "✗"
    print(
        f"labelled {s['labelled']} (grounded {s['grounded']}, not grounded {s['not_grounded']}), "
        f"skipped {s['skipped']} · target 30-50 {target}"
    )
    return 0


def _golden_blind(cfg: Config, args: argparse.Namespace) -> int:
    from enigram.application.evaluation.blind import write_blind_sheet

    run = write_blind_sheet(cfg, args.answers, args.seed)
    print(f"sheet: {run.sheet_path}")
    print(f"key:   {run.key_path} (open only after labelling)")
    print(f"{run.questions} questions · {run.systems} systems")
    return 0


def _golden_unblind(sheet: Path) -> int:
    from enigram.application.evaluation.blind import unblind

    for mode, n in unblind(sheet).items():
        judged = n["correct"] + n["wrong"]
        share = f" ({n['correct'] / judged:.0%})" if judged else ""
        print(f"{mode:<12} correct {n['correct']}/{judged}{share} · skipped {n['skipped']}")
    return 0


async def _eval(cfg: Config, args: argparse.Namespace) -> int:
    from enigram.application.evaluation.evaluate import run_eval
    from enigram.bootstrap import build_deps

    run = await run_eval(
        cfg, build_deps(cfg), args.answers, args.blind, args.labels, judge=args.judge
    )
    for criterion, verdict in run.verdicts.items():
        print(f"{criterion}: {verdict}")
    print(f"report: {run.report_path}")
    # A hypothesis that fails is a result, not an error.
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = Config.from_env()
        _setup_logging(cfg.log_level)
        if args.command == "index" and args.pages:
            cfg = dataclasses.replace(
                cfg, data_dir=cfg.data_dir / "dev", workspace=f"{cfg.workspace}_dev"
            )
        if args.command == "check":
            return asyncio.run(_check(cfg))
        if args.command == "index":
            return asyncio.run(_index(cfg, args))
        if args.command == "query":
            return asyncio.run(_query(cfg, args))
        if args.command == "repo":
            return asyncio.run(_repo(cfg, args))
        if args.command == "mcp":
            from enigram.adapters.driving.mcp_server import create_server

            # Fail with a readable error before the host only sees a server that died.
            asyncio.run(_preflight(cfg))
            create_server(cfg).run()  # blocks; runs its own event loop, stdout is the wire
            return 0
        if args.command == "api":
            import uvicorn

            from enigram.adapters.driving.http_api import create_app

            # log_config=None keeps _setup_logging instead of uvicorn's own config.
            uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_config=None)
            return 0
        if args.command == "worker":
            from enigram.adapters.driving.worker import run_worker

            try:
                asyncio.run(run_worker(cfg))
            except KeyboardInterrupt:
                pass
            return 0
        if args.command == "golden":
            if args.golden_command == "check":
                return _golden_check(cfg, args.file, args.graph)
            if args.golden_command == "citations":
                return asyncio.run(_golden_citations(cfg, args.answers))
            if args.golden_command == "answer":
                return asyncio.run(_golden_answer(cfg, args))
            if args.golden_command == "blind":
                return _golden_blind(cfg, args)
            if args.golden_command == "unblind":
                return _golden_unblind(args.sheet)
            return _golden_labels(args.sheet)
        if args.command == "eval":
            return asyncio.run(_eval(cfg, args))
        from enigram.application.report import build_report
        from enigram.bootstrap import build_deps

        graph = asyncio.run(build_deps(cfg).load_graph())
        print(f"report: {build_report(cfg, graph, review_file=args.review)}")
        return 0
    except EnigramError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
