import shutil
import subprocess
from pathlib import Path

import pytest

from enigram.adapters.driven import ts_graph_sidecar
from enigram.adapters.driven.ts_graph_sidecar import TsGraphSidecar
from enigram.domain.errors import EnigramError

FIXTURE = Path(__file__).resolve().parents[3] / "tools" / "ts-graph" / "test" / "fixture"


@pytest.mark.skipif(
    shutil.which("node") is None or not (FIXTURE.parents[1] / "node_modules").exists(),
    reason="node or tools/ts-graph/node_modules missing (npm ci in tools/ts-graph)",
)
def test_sidecar_parses_the_fixture(cfg):
    graph = TsGraphSidecar(cfg).parse(FIXTURE)

    names = {s.qualified_name for s in graph.symbols}
    assert {"AppController.find", "AppService.find", "UserRepo.load", "SqlUserRepo"} <= names
    assert not any(s.file.endswith(".spec.ts") for s in graph.symbols)
    routes = [(e.kind, e.route) for e in graph.entrypoints]
    assert ("http_route", "GET /users/:id") in routes and ("cli_main", "bootstrap") in routes
    calls = {(e.src.split("#")[1], e.dst.split("#")[-1]) for e in graph.edges if e.kind == "calls"}
    assert ("AppController.find", "AppService.find") in calls
    assert ("AppService.find", "UserRepo.load") in calls
    external = [e for e in graph.edges if e.kind == "calls_external" and e.dst == "bcrypt.compare"]
    assert external and not external[0].resolved
    env = {e.dst for e in graph.edges if e.kind == "reads_env"}
    assert {"LIMIT", "db.type"} <= env
    assert graph.report["files_skipped"]["test"] == 1


def test_sidecar_reports_the_last_stderr_line(cfg, tmp_path, monkeypatch):
    (tmp_path / "index.mjs").write_text("")
    done = subprocess.CompletedProcess([], 2, "", "warning\nts-graph: cannot read tsconfig.json\n")
    monkeypatch.setattr(ts_graph_sidecar.subprocess, "run", lambda *a, **kw: done)
    sidecar = TsGraphSidecar(cfg)
    sidecar.dir = tmp_path

    with pytest.raises(EnigramError, match="ts-graph failed: ts-graph: cannot read tsconfig"):
        sidecar.parse(tmp_path)


def test_sidecar_runs_from_its_own_dir_with_a_minimal_env(cfg, tmp_path, monkeypatch):
    (tmp_path / "index.mjs").write_text("")
    seen = {}

    def fake_run(command, **kw):
        seen.update(command=command, **kw)
        out = Path(command[command.index("--out") + 1])
        for name in ("symbols.jsonl", "edges.jsonl", "entrypoints.jsonl"):
            (out / name).write_text("")
        (out / "report.json").write_text("{}")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(ts_graph_sidecar.subprocess, "run", fake_run)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key-not-real")
    sidecar = TsGraphSidecar(cfg)
    sidecar.dir = tmp_path
    repo = tmp_path / "repo"

    graph = sidecar.parse(repo)

    assert graph.symbols == [] and graph.report == {}
    assert seen["cwd"] == str(tmp_path)
    assert seen["command"][2:4] == ["--repo", str(repo.resolve())]
    assert set(seen["env"]) == {"PATH", "HOME", "NODE_OPTIONS"}  # no API keys for the sidecar
    assert seen["timeout"] == ts_graph_sidecar.SIDECAR_TIMEOUT_SECS


def test_sidecar_missing_points_at_ts_graph_dir(cfg, tmp_path):
    sidecar = TsGraphSidecar(cfg)
    sidecar.dir = tmp_path / "nowhere"
    with pytest.raises(EnigramError, match="TS_GRAPH_DIR"):
        sidecar.parse(tmp_path)
