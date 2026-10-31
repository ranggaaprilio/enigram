"""CodeParser on tools/ts-graph: one `node` process per repository, JSONL back."""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from enigram.application.ports import ParsedGraph
from enigram.config import Config
from enigram.domain.code import parse_edge_row, parse_entrypoint_row, parse_symbol_row
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

SIDECAR_TIMEOUT_SECS = 600
SIDECAR_HEAP_MB = 2048


def _rows(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


class TsGraphSidecar:
    def __init__(self, cfg: Config) -> None:
        self.dir = cfg.ts_graph_dir

    def parse(self, root: Path) -> ParsedGraph:
        entry = self.dir / "index.mjs"
        if not entry.exists():
            raise EnigramError(
                f"ts-graph sidecar not found at {self.dir}; run `npm ci` in tools/ts-graph "
                "or set TS_GRAPH_DIR"
            )
        node = shutil.which("node") or "node"
        start = time.perf_counter()
        with tempfile.TemporaryDirectory() as out:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": out,
                "NODE_OPTIONS": f"--max-old-space-size={SIDECAR_HEAP_MB}",
            }
            try:
                # cwd = the sidecar, so Node resolves ts-morph from its node_modules; the
                # repository is only an argument, never the working directory.
                done = subprocess.run(
                    [node, str(entry), "--repo", str(root.resolve()), "--out", out],
                    capture_output=True,
                    text=True,
                    timeout=SIDECAR_TIMEOUT_SECS,
                    env=env,
                    cwd=str(self.dir),
                    check=False,
                )
            except subprocess.TimeoutExpired as e:
                raise EnigramError(f"ts-graph took longer than {SIDECAR_TIMEOUT_SECS} s") from e
            except OSError as e:
                raise EnigramError(f"ts-graph could not start node: {e}") from e
            if done.returncode != 0:
                lines = done.stderr.strip().splitlines()
                raise EnigramError(f"ts-graph failed: {lines[-1] if lines else done.returncode}")
            out_dir = Path(out)
            try:
                symbols = [parse_symbol_row(r) for r in _rows(out_dir / "symbols.jsonl")]
                edges = [parse_edge_row(r) for r in _rows(out_dir / "edges.jsonl")]
                entrypoints = [
                    parse_entrypoint_row(r) for r in _rows(out_dir / "entrypoints.jsonl")
                ]
                report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise EnigramError(f"ts-graph wrote unreadable output: {e}") from e
        log.info(
            "[code] sidecar: %d symbols, %d edges, %d entry points (%.1fs)",
            len(symbols),
            len(edges),
            len(entrypoints),
            time.perf_counter() - start,
        )
        return ParsedGraph(symbols, edges, entrypoints, report)
