import dataclasses
import shutil
import subprocess

import pytest
from code_fakes import FakeChat, FakeCodeGraph, fixture_graph, sym
from git_fixture import git_commit
from git_fixture import git_repo as _git_repo

from enigram.application import code_index
from enigram.application.code_index import index_code, redact_graph, repo_limits
from enigram.application.ports import ParsedGraph
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
    assert repo_limits(tmp_path, 2000, 50_000) == (2, 3)


@pytest.mark.parametrize(
    ("max_files", "max_loc", "match"),
    [
        (1, 50_000, "More than 1 source files"),
        (2000, 2, "More than 2 lines"),
    ],
)
def test_repo_limits_reject_large_repositories(tmp_path, max_files, max_loc, match):
    for rel, text in TREE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    with pytest.raises(EnigramError, match=match):
        repo_limits(tmp_path, max_files, max_loc)


def test_repo_limits_zero_means_no_limit(tmp_path):
    for rel, text in TREE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    assert repo_limits(tmp_path, 0, 0) == (2, 3)


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
    # Every symbol's text plus every summary is embedded (FakeEmbedder from the deps fixture).
    texts = result["symbols"] + len(graph_repo.stored)
    assert result["embeddings"]["embedded"] == texts == len(graph_repo.embeddings)
    assert stored["report"]["embeddings"]["model"] == "fake-code-1"
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
async def test_index_code_checks_limits_before_the_sidecar(cfg, deps, tmp_path):
    root = _git_repo(tmp_path / "repo", TREE)
    parser = FakeParser(fixture_graph())
    deps.code_parser = lambda: parser
    cfg = dataclasses.replace(cfg, code_max_files=1)

    with pytest.raises(EnigramError, match="More than 1 source files"):
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
    # Only in node_modules or a test folder: a package's or a fixture's, not the repository's.
    for folder in ("node_modules/pkg", "tools/x/test/fixture"):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "tsconfig.json").write_text("{}")
    assert not code_index.is_ts_repo(tmp_path)
    # A monorepo's project counts (plan item 9), as does a root one.
    (tmp_path / "apps/web").mkdir(parents=True)
    (tmp_path / "apps/web/tsconfig.json").write_text("{}")
    assert code_index.is_ts_repo(tmp_path)


def _tree(root, tree):
    for rel, text in tree.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


@pytest.mark.parametrize(
    ("tree", "expected"),
    [
        ({"package.json": "{}", "src/app.js": "x()\n"}, True),
        ({"web/jsconfig.json": "{}", "web/src/App.jsx": "<A />\n"}, True),
        ({"package.json": "{}", "index.mjs": "x()\n"}, True),
        # A file in a subfolder belongs to the nearest package.json above it.
        ({"package.json": "{}", "server/app.cjs": "x()\n"}, True),
        # Markdown and a package.json (scripts only): no code.
        ({"package.json": "{}", "README.md": "# App\n"}, False),
        # JavaScript but no package.json or jsconfig.json: a folder of scripts.
        ({"scripts/run.js": "x()\n"}, False),
        # JavaScript outside the only project: the sidecar leaves it out (no_project).
        ({"web/package.json": "{}", "scripts/run.js": "x()\n"}, False),
        # Only tests, fixtures, a tool's config, a minified bundle, or a dependency's code.
        ({"package.json": "{}", "test/app.test.js": "x()\n"}, False),
        ({"package.json": "{}", "src/app.test.js": "x()\n"}, False),
        ({"package.json": "{}", "src/fixtures/a.js": "x()\n"}, False),
        ({"package.json": "{}", "webpack.config.js": "x()\n", ".eslintrc.js": "x\n"}, False),
        ({"package.json": "{}", "public/vendor.min.js": "x()\n"}, False),
        ({"package.json": "{}", "public/app.bundle.mjs": "x()\n"}, False),
        ({"package.json": "{}", "node_modules/pkg/index.js": "x()\n"}, False),
        ({"node_modules/pkg/package.json": "{}", "src/app.js": "x()\n"}, False),
    ],
)
def test_is_js_repo(tmp_path, tree, expected):
    assert code_index.is_js_repo(_tree(tmp_path, tree)) is expected


def test_is_js_repo_ignores_symbolic_links(tmp_path):
    outside = _tree(tmp_path / "outside", {"package.json": "{}", "a.js": "x()\n"})
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").symlink_to(outside / "package.json")
    (repo / "a.js").symlink_to(outside / "a.js")
    assert not code_index.is_js_repo(repo)
    (repo / "package.json").unlink()
    (repo / "package.json").write_text("{}")
    assert not code_index.is_js_repo(repo)  # the code is still a link


def test_repo_limits_count_javascript_too(tmp_path):
    _tree(
        tmp_path,
        {
            "src/a.ts": "a\n",
            "src/b.js": "b\nb\n",
            "src/c.jsx": "c\n",
            "src/d.mjs": "d\n",
            "src/e.cjs": "e\n",
            "public/vendor.min.js": "v\n" * 50,
            "public/app.bundle.js": "v\n" * 50,
            "node_modules/pkg/index.js": "n\n",
            # Counted, as test TypeScript always was: the limit is about the clone's size.
            "test/f.test.js": "t\n",
        },
    )
    assert repo_limits(tmp_path, 2000, 50_000) == (6, 7)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_blob_shas_include_javascript(tmp_path):
    root = _git_repo(
        tmp_path / "repo",
        {"a.ts": "a\n", "b.js": "b\n", "c.cjs": "c\n", "d.jsx": "d\n", "README.md": "# R\n"},
    )
    assert {f.path for f in code_index.git_blob_shas(root)} == {"a.ts", "b.js", "c.cjs", "d.jsx"}


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_index_code_warns_about_an_empty_graph(cfg, deps, tmp_path):
    root = _git_repo(tmp_path / "repo", {"package.json": "{}", "src/a.js": "a\n"})
    empty = ParsedGraph([], [], [], {"files_skipped": {"syntax": 1}})
    deps.code_parser = lambda: FakeParser(empty)
    deps.generator = lambda: FakeChat()

    graph_repo = FakeCodeGraph()

    result = await index_code(cfg, deps, graph_repo, root, URL, None)

    assert result["symbols"] == 0
    assert "No symbols" in result["warning"] and "files_skipped" in result["warning"]
    assert graph_repo.repos[result["repo_id"]]["report"]["warning"] == result["warning"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_index_code_no_warning_with_symbols(cfg, deps, tmp_path):
    root = _git_repo(tmp_path / "repo", TREE)
    deps.code_parser = lambda: FakeParser(fixture_graph())
    deps.generator = lambda: FakeChat()

    result = await index_code(cfg, deps, FakeCodeGraph(), root, URL, None)

    assert "warning" not in result
