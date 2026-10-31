import os
from pathlib import Path

import pytest

from enigram.config import Config

CONFIG_ENV = (
    "OMLX_BASE_URL",
    "OMLX_API_KEY",
    "LLM_PROVIDER",
    "DEEPSEEK_API_KEY",
    "LLM_MODEL",
    "JUDGE_PROVIDER",
    "JUDGE_MODEL",
    "JUDGE_MAX_TOKENS",
    "EMBED_MODEL",
    "THINKING_MODE",
    "DATA_DIR",
    "STORAGE_BACKEND",
    "WORKSPACE",
    "POSTGRES_WORKSPACE",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DATABASE",
    "ENIGRAM_API_KEYS",
    "REPO_HOSTS",
)


def pytest_collection_modifyitems(config, items):
    laya_path = Path(
        os.environ.get("LAYA_PATH", "~/.omlx/models/convaiinnovations/laya")
    ).expanduser()
    skips = {
        "omlx": None if os.environ.get("OMLX_API_KEY") else "OMLX_API_KEY not set",
        "laya": None if (laya_path / "model.safetensors").exists() else f"no Laya at {laya_path}",
        "postgres": (
            None if os.environ.get("ENIGRAM_TEST_PG_DSN") else "ENIGRAM_TEST_PG_DSN not set"
        ),
    }
    for item in items:
        for marker, reason in skips.items():
            if reason and marker in item.keywords:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(autouse=True)
def reset_lightrag_shared_storage():
    # LightRAG keeps storage data in process-global state keyed by namespace, so a second
    # LightRAG in the same process sees the first one's documents as already processed.
    yield
    from lightrag.kg.shared_storage import finalize_share_data

    finalize_share_data()


@pytest.fixture
def clean_env(monkeypatch):
    for name in CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)
    # Point load_dotenv at a missing file so a developer's .env never leaks into unit tests.
    return Path("/nonexistent/.env")


@pytest.fixture
def cfg(clean_env, monkeypatch, tmp_path) -> Config:
    monkeypatch.setenv("OMLX_API_KEY", "test-key-not-real")
    monkeypatch.setenv("LLM_MODEL", "llm-test")
    monkeypatch.setenv("EMBED_MODEL", "embed-test")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return Config.from_env(clean_env)


@pytest.fixture
def deepseek_cfg(cfg, clean_env, monkeypatch) -> Config:
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key-not-real")
    monkeypatch.delenv("LLM_MODEL")
    return Config.from_env(clean_env)


@pytest.fixture
def deps(cfg):
    """The real adapters for cfg; tests replace single fields with fakes."""
    from enigram.bootstrap import build_deps

    return build_deps(cfg)


@pytest.fixture
def pg_cfg(cfg, clean_env, monkeypatch) -> Config:
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_USER", "enigram")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pg-password-not-real")
    monkeypatch.setenv("POSTGRES_DATABASE", "enigram")
    return Config.from_env(clean_env)


def make_book_pdf(path: Path) -> Path:
    """Three-page PDF: title + Chapter 1 on pages 1-2, Chapter 2 and References on page 3."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

    styles = getSampleStyleSheet()
    body = (
        "The Lighthouse Protocol coordinates ships in the northern harbor. "
        "Captain Mira Okafor designed it after the storm of 1987. "
    ) * 12
    story = [
        Paragraph("Harbor Signals Study", styles["Title"]),
        Paragraph(
            "Ada Lighthouse, Harbor Institute of Navigation, Norway, ada.lighthouse@harbor.edu. "
            "Ben Signal, Fjord University, Norway, ben.signal@fjord.edu",
            styles["BodyText"],
        ),
        Paragraph("Chapter 1 Harbor Signals", styles["Heading1"]),
        Paragraph(body, styles["BodyText"]),
        PageBreak(),
        Paragraph(body, styles["BodyText"]),
        PageBreak(),
        Paragraph("Chapter 2 The Storm Archive", styles["Heading1"]),
        Paragraph(
            "The Storm Archive records every vessel guided by the Lighthouse Protocol. " * 10,
            styles["BodyText"],
        ),
        Paragraph("References", styles["Heading1"]),
        Paragraph(
            "[1] Zanzibar Quillfeather. Tidal Cartography Methods. Journal of Harbors, 1999.",
            styles["BodyText"],
        ),
        Paragraph(
            "[2] Zanzibar Quillfeather and Ola Brook. Fog Signals. Maritime Letters, 2003.",
            styles["BodyText"],
        ),
    ]
    SimpleDocTemplate(str(path), pagesize=A4).build(story)
    return path


@pytest.fixture
def book_pdf(tmp_path) -> Path:
    return make_book_pdf(tmp_path / "harbor-book.pdf")


@pytest.fixture
def golden_env(cfg, tmp_path, monkeypatch):
    """Two indexed fake papers, a golden set (q01 single_hop, q02 multi_section) and a function
    that installs a fake LightRAG."""
    from test_golden import INTRO, METHOD, Q1, Q2, question_yaml, write_golden, write_index

    from enigram.adapters.driven import lightrag_engine

    write_index(cfg)
    cfg.rag_dir.mkdir(parents=True)
    (cfg.rag_dir / "graph_chunk_entity_relation.graphml").write_text("<graphml/>")
    path = write_golden(
        tmp_path / "golden.yaml",
        "questions:\n"
        + question_yaml("single_hop", INTRO, qid="q01", question=Q1)
        + question_yaml("multi_section", INTRO, METHOD, qid="q02", question=Q2),
    )

    def install(rag):
        monkeypatch.setattr(lightrag_engine, "build_rag", lambda cfg, **kw: rag)
        return rag

    return path, install
