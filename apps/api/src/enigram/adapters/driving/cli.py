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

    sub.add_parser("check", help="verify OMLX models, thinking mode and embedding dim")

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

    sub.add_parser("mcp", help="run the MCP server on stdio (for Claude Code)")

    api = sub.add_parser("api", help="run the HTTP API (upload, query, query traces)")
    api.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 in a container)")
    api.add_argument("--port", type=int, default=8080)

    sub.add_parser("worker", help="index uploaded documents from the job queue")

    golden = sub.add_parser("golden", help="golden set: check questions, answer them, count labels")
    gsub = golden.add_subparsers(dest="golden_command", required=True)
    check = gsub.add_parser("check", help="validate the golden set against the index")
    answer = gsub.add_parser("answer", help="answer every golden question and write a label sheet")
    for p in (check, answer):
        p.add_argument("--file", type=Path, default=Path("eval/golden.yaml"), help="golden YAML")
    answer.add_argument(
        "--mode", choices=("mix", "naive"), default="mix", help="system to answer with"
    )
    answer.add_argument("--limit", type=int, help="only the first N questions (quick trial)")
    labels = gsub.add_parser("labels", help="count human claim labels in a label sheet")
    labels.add_argument("sheet", type=Path, help="data/eval/labels-<mode>-<ts>.md")

    evaluate = sub.add_parser(
        "eval", help="evaluate GraphRAG and the vector baseline on the golden set (~1 h)"
    )
    evaluate.add_argument("--file", type=Path, default=Path("eval/golden.yaml"), help="golden YAML")
    evaluate.add_argument(
        "--labels",
        type=Path,
        nargs="+",
        default=[],
        metavar="SHEET",
        help="human claim label sheets (labels-<mode>-<ts>.md) to measure the judges against",
    )
    evaluate.add_argument("--limit", type=int, help="only the first N questions (quick trial)")
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
    print(f"✓ models served: {', '.join(result.models)}")
    think = "✗ raw output still contains <think>" if result.raw_had_think else "✓ no <think>"
    print(f"{think} (thinking_mode={cfg.thinking_mode}, {result.completion_secs:.1f}s)")
    print("✓ JSON mode works" if result.json_mode_ok else "✗ JSON mode failed (see log)")
    print(f"✓ embedding dim {result.embed_dim}")
    return 0 if result.json_mode_ok and not result.raw_had_think else 1


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


async def _query(cfg: Config, question: str, mode: str) -> int:
    from enigram.application.querying import answer_question
    from enigram.bootstrap import build_deps

    async with build_deps(cfg).open_engine() as engine:
        result = await answer_question(engine, question, mode)
    print(result.answer)
    if result.sources:
        print("\nSources:")
        for s in result.sources:
            print(f"  [{s.ref}] {s.citation.rsplit(' | #', 1)[0]}")
    print(f"({result.mode}, {result.query_secs:.1f}s)")
    return 0 if result.found else 1


def _golden_check(cfg: Config, path: Path) -> int:
    from enigram.application.evaluation.golden import (
        CATEGORIES,
        TARGET_PER_CATEGORY,
        TARGET_UNANSWERABLE,
        UNANSWERABLE,
        check_golden,
    )

    questions, problems = check_golden(cfg, path)
    for q in questions:
        if q.id in problems:
            print(f"{q.id:<6} {q.category:<14} ✗")
            for problem in problems[q.id]:
                print(f"         {problem}")
        else:
            print(f"{q.id:<6} {q.category:<14} ok  {' '.join(ev.chunk_id for ev in q.evidence)}")
    if not questions:
        print(f"{path}: no questions yet")
    papers = {ev.book for q in questions for ev in q.evidence if ev.book}
    counts = []
    for cat in CATEGORIES:
        n = sum(q.category == cat for q in questions)
        note = " (needs ≥2 indexed papers)" if cat == "multi_paper" and len(papers) < 2 else ""
        target = TARGET_UNANSWERABLE if cat == UNANSWERABLE else TARGET_PER_CATEGORY
        counts.append(f"{cat} {n}/{target}{note}")
    print(" · ".join(counts))
    return 1 if problems or not questions else 0


async def _golden_answer(cfg: Config, args: argparse.Namespace) -> int:
    from enigram.application.evaluation.golden_run import run_golden
    from enigram.bootstrap import build_deps

    run = await run_golden(cfg, build_deps(cfg), args.file, args.mode, args.limit)
    print(f"answers: {run.answers_path}")
    print(f"labels:  {run.labels_path}")
    if run.failed:
        print(f"{run.failed} question(s) failed; see the answers file")
    return 1 if run.failed else 0


def _golden_labels(sheet: Path) -> int:
    from enigram.application.evaluation.claims import count_claims, label_summary, read_labels

    s = label_summary(read_labels(sheet), count_claims(sheet))
    target = "✓" if s["labelled"] >= 30 else "✗"
    print(
        f"labelled {s['labelled']} (grounded {s['grounded']}, not grounded {s['not_grounded']}), "
        f"skipped {s['skipped']} · target 30-50 {target}"
    )
    return 0


async def _eval(cfg: Config, args: argparse.Namespace) -> int:
    from enigram.application.evaluation.evaluate import run_eval
    from enigram.bootstrap import build_deps

    run = await run_eval(cfg, build_deps(cfg), args.file, args.labels, args.limit)
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
            return asyncio.run(_query(cfg, args.question, args.mode))
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
                return _golden_check(cfg, args.file)
            if args.golden_command == "answer":
                return asyncio.run(_golden_answer(cfg, args))
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
