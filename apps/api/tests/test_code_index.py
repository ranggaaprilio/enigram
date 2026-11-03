import dataclasses
import shutil
import subprocess

import pytest
from code_fakes import FakeChat, FakeCodeGraph, fixture_graph, sym
from git_fixture import git_commit
from git_fixture import git_repo as _git_repo

from enigram.application import code_index
from enigram.application.code_index import index_code, redact_graph, repo_limits
from enigram.domain.errors import EnigramError

URL = "https://github.com/org/app"
TREE = {
    "tsconfig.json": "{}",
    "src/app.service.ts": "export class AppService {}\n",
    "src/app.controller.ts": "export class AppController {}\n// two\n",
    "src/types.d.ts": "declare const x: number;\n",
    "node_modules/pkg/index.ts": "export const y = 1;\n",
    "README.md": "# App\n",
}


class FakeParser:
    def __init__(self, graph):
        self.graph = graph
        self.roots = []

    def parse(self, root):
        self.roots.append(root)
        return self.graph


def test_repo_limits_counts_ts_files_and_lines(tmp_path):
    for rel, text in TREE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    # app.service.ts (1 line) + app.controller.ts (2 lines); no .d.ts, no node_modules
    assert repo_limits(tmp_path) == (2, 3)


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("CODE_MAX_FILES", 1, "More than 1 TypeScript files"),
        ("CODE_MAX_LOC", 2, "More than 2 lines"),
    ],
)
def test_repo_limits_reject_large_repositories(tmp_path, monkeypatch, name, value, match):
    for rel, text in TREE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    monkeypatch.setattr(code_index, name, value)
    with pytest.raises(EnigramError, match=match):
        repo_limits(tmp_path)


def test_redact_graph_drops_secret_files_and_redacts_bodies():
    graph = fixture_graph()
    leaky = sym(
        "Keys.load",
        body="load() { return 'AKIAABCDEFGHIJKLMNOP'; }",
        decorators_text=("@Key('AKIAABCDEFGHIJKLMNOQ')",),
    )
    secret = sym("creds", kind="variable", file="config/credentials.json", parent="")
    parsed = dataclasses.replace(graph, symbols=[*graph.symbols, leaky, secret])

    out, n = redact_graph(parsed)

    ids = {s.id for s in out.symbols}
    assert secret.id not in ids
    redacted = next(s for s in out.symbols if s.id == leaky.id)
    assert "[REDACTED]" in redacted.body
    assert "AKIA" not in redacted.decorators_text[0]
    assert n == 3  # two values redacted + one symbol dropped
    assert len(out.symbols) == len(graph.symbols) + 1


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_index_code_stores_graph_blobs_and_summaries(cfg, deps, tmp_path):
    root = _git_repo(tmp_path / "repo", TREE)
    graph_repo, parser, chat = FakeCodeGraph(), FakeParser(fixture_graph()), FakeChat()
    deps.code_parser = lambda: parser
    deps.generator = lambda: chat

    result = await index_code(cfg, deps, graph_repo, root, URL, "main")

    assert parser.roots == [root]
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert result["commit_sha"] == head
    assert (result["files"], result["loc"]) == (2, 3)
    assert (result["symbols"], result["entrypoints"]) == (8, 2)
    assert result["calls_resolved_share"] == 0.75
    assert result["summaries"]["flows"] == 2
    stored = graph_repo.repos[result["repo_id"]]
    assert stored["indexed_sha"] == head
    assert stored["report"]["summaries"]["flows"] == 2  # merged into the stored report
    blobs = {f.path for f in graph_repo.blobs[result["repo_id"]]}
    # Every TypeScript blob at HEAD, the baseline for a later diff.
    assert blobs == {
        "src/app.service.ts",
        "src/app.controller.ts",
        "src/types.d.ts",
        "node_modules/pkg/index.ts",
    }
    assert all(len(f.blob_sha) == 40 for f in graph_repo.blobs[result["repo_id"]])


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_index_code_checks_limits_before_the_sidecar(cfg, deps, tmp_path, monkeypatch):
    root = _git_repo(tmp_path / "repo", TREE)
    parser = FakeParser(fixture_graph())
    deps.code_parser = lambda: parser
    monkeypatch.setattr(code_index, "CODE_MAX_FILES", 1)

    with pytest.raises(EnigramError, match="More than 1 TypeScript files"):
        await index_code(cfg, deps, FakeCodeGraph(), root, URL, None)
    assert parser.roots == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_second_index_reports_the_diff_and_symbol_changes(cfg, deps, tmp_path):
    root = _git_repo(tmp_path / "repo", TREE)
    first_graph = fixture_graph()
    parser, graph_repo = FakeParser(first_graph), FakeCodeGraph()
    deps.code_parser, deps.generator = (lambda: parser), FakeChat
    first = await index_code(cfg, deps, graph_repo, root, URL, None)
    assert (first["files_changed"], first["symbols_changed"], first["staleness"]) == (4, 0, {})
    assert graph_repo.changes == []  # the first index is not a change
    assert first["commit_at"][:4].isdigit()

    service = "src/app.service.ts#AppService.find"
    parser.graph = dataclasses.replace(
        first_graph,
        symbols=[
            dataclasses.replace(s, body_hash="new") if s.id == service else s
            for s in first_graph.symbols
        ],
    )
    sha = git_commit(root, {"src/app.service.ts": "export class AppService { x = 1 }\n"}, "c")
    second = await index_code(cfg, deps, graph_repo, root, URL, None)

    assert (second["files_changed"], second["files_affected"], second["symbols_changed"]) == (
        1,
        1,
        1,
    )
    assert graph_repo.changes == [(1, sha, service, "h-AppService.find", "new")]


def test_is_ts_repo(tmp_path):
    assert not code_index.is_ts_repo(tmp_path)
    (tmp_path / "tsconfig.json").write_text("{}")
    assert code_index.is_ts_repo(tmp_path)
