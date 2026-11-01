import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

from enigram.domain.errors import EnigramError

# LightRAG re-chunks anything longer than this. Docling chunks are capped at
# CHUNK_MAX_TOKENS and both Docling and LightRAG count with tiktoken (cl100k_base), so the
# 1.5x margin is only a safety net that keeps one Docling chunk == one LightRAG chunk.
LIGHTRAG_CHUNK_TOKEN_SIZE = 1200

# off sends thinking.type=disabled to DeepSeek; on leaves its thinking on.
THINKING_MODES = ("off", "on")

# Chat completions; embeddings are on Voyage (DeepSeek serves no embedding model).
LLM_PROVIDERS = ("deepseek",)
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
DEFAULT_EMBED_MODEL = "voyage-4-lite"
# The Docling chunker's tokenizer, recorded in the manifest by guard_chunk_settings.
CHUNK_TOKENIZER = "tiktoken:cl100k_base"

# Where LightRAG keeps the index: JSON/GraphML files under rag_dir, or PostgreSQL (pgvector).
STORAGE_BACKENDS = ("file", "postgres")

MIN_API_KEY_LENGTH = 16

# Hosts `index_repo` may clone from (https only), so a key holder cannot make the worker fetch
# from the private network.
DEFAULT_REPO_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise EnigramError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise EnigramError(f"{name} must be an integer, got {raw!r}") from e


def _api_keys(raw: str) -> tuple[tuple[str, str], ...]:
    """`name:key,name2:key2` -> ((name, key), ...); errors name the item, never the key."""
    keys: list[tuple[str, str]] = []
    for pos, item in enumerate((i.strip() for i in raw.split(",")), start=1):
        if not item:
            continue
        name, sep, key = (part.strip() for part in item.partition(":"))
        if not sep or not name or not key:
            raise EnigramError(f"ENIGRAM_API_KEYS item {pos} must be name:key")
        if len(key) < MIN_API_KEY_LENGTH:
            raise EnigramError(
                f"ENIGRAM_API_KEYS key {name!r} is shorter than {MIN_API_KEY_LENGTH} characters"
            )
        if any(name == n for n, _ in keys):
            raise EnigramError(f"ENIGRAM_API_KEYS has the name {name!r} twice")
        if any(key == k for _, k in keys):
            raise EnigramError(f"ENIGRAM_API_KEYS key {name!r} repeats an earlier key")
        keys.append((name, key))
    return tuple(keys)


@dataclass(frozen=True)
class Config:
    llm_provider: str
    llm_base_url: str
    llm_api_key: str = field(repr=False)
    llm_model: str
    embed_model: str
    embed_dim: int
    embed_max_tokens: int
    voyage_api_key: str = field(repr=False)
    chunk_max_tokens: int
    llm_max_async: int
    llm_max_tokens: int
    judge_provider: str
    judge_base_url: str
    judge_api_key: str = field(repr=False)
    judge_model: str
    judge_max_tokens: int
    max_parallel_insert: int
    thinking_mode: str
    storage: str
    postgres_database: str
    workspace: str
    # (name, key) pairs from ENIGRAM_API_KEYS; empty means the API is open.
    api_keys: tuple[tuple[str, str], ...] = field(repr=False)
    repo_hosts: tuple[str, ...]
    data_dir: Path
    log_level: str
    # The ts-morph sidecar (tools/ts-graph) that parses TypeScript repositories.
    ts_graph_dir: Path

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> "Config":
        load_dotenv(dotenv_path)
        thinking_mode = os.environ.get("THINKING_MODE", "off").strip() or "off"
        if thinking_mode not in THINKING_MODES:
            raise EnigramError(
                f"THINKING_MODE must be one of {THINKING_MODES}, got {thinking_mode!r}"
            )
        llm_provider = os.environ.get("LLM_PROVIDER", "deepseek").strip() or "deepseek"
        if llm_provider not in LLM_PROVIDERS:
            raise EnigramError(f"LLM_PROVIDER must be one of {LLM_PROVIDERS}, got {llm_provider!r}")

        def endpoint(provider: str, model_var: str, fallback_model: str = "") -> tuple[str, ...]:
            model = os.environ.get(model_var, "").strip() or fallback_model
            return DEEPSEEK_BASE_URL, _required("DEEPSEEK_API_KEY"), model or DEEPSEEK_DEFAULT_MODEL

        llm_base_url, llm_api_key, llm_model = endpoint(llm_provider, "LLM_MODEL")
        # The judge defaults to the generator; set JUDGE_* so a model never grades its own answers.
        judge_provider = os.environ.get("JUDGE_PROVIDER", "").strip() or llm_provider
        if judge_provider not in LLM_PROVIDERS:
            raise EnigramError(
                f"JUDGE_PROVIDER must be one of {LLM_PROVIDERS}, got {judge_provider!r}"
            )
        judge_base_url, judge_api_key, judge_model = endpoint(
            judge_provider, "JUDGE_MODEL", llm_model if judge_provider == llm_provider else ""
        )
        llm_max_tokens = _int("LLM_MAX_TOKENS", 2048)
        storage = os.environ.get("STORAGE_BACKEND", "file").strip() or "file"
        if storage not in STORAGE_BACKENDS:
            raise EnigramError(
                f"STORAGE_BACKEND must be one of {STORAGE_BACKENDS}, got {storage!r}"
            )
        if storage == "postgres":
            # LightRAG reads the POSTGRES_* variables itself; check them here for a clear error.
            for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DATABASE"):
                _required(name)
        if os.environ.get("POSTGRES_WORKSPACE", "").strip():
            # LightRAG lets it override the workspace passed in code, which would put the
            # `index --pages` dev run into the main index.
            raise EnigramError("Use WORKSPACE instead of POSTGRES_WORKSPACE.")
        return cls(
            llm_provider=llm_provider,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            embed_model=os.environ.get("EMBED_MODEL", "").strip() or DEFAULT_EMBED_MODEL,
            embed_dim=_int("EMBED_DIM", 1024),
            embed_max_tokens=_int("EMBED_MAX_TOKENS", 32000),
            voyage_api_key=_required("VOYAGE_API_KEY"),
            chunk_max_tokens=_int("CHUNK_MAX_TOKENS", 800),
            llm_max_async=_int("LLM_MAX_ASYNC", 2),
            # Extraction answers run 600-1000 tokens; the cap stops runaway repetition well
            # before LightRAG's 240 s per-call timeout (~18 tok/s with two parallel calls).
            llm_max_tokens=llm_max_tokens,
            judge_provider=judge_provider,
            judge_base_url=judge_base_url,
            judge_api_key=judge_api_key,
            judge_model=judge_model,
            # A reasoning judge spends max_tokens on its reasoning too.
            judge_max_tokens=_int("JUDGE_MAX_TOKENS", llm_max_tokens),
            max_parallel_insert=_int("MAX_PARALLEL_INSERT", 2),
            thinking_mode=thinking_mode,
            storage=storage,
            postgres_database=os.environ.get("POSTGRES_DATABASE", "").strip(),
            workspace=os.environ.get("WORKSPACE", "").strip() or "default",
            api_keys=_api_keys(os.environ.get("ENIGRAM_API_KEYS", "")),
            repo_hosts=tuple(
                h.strip().lower() for h in os.environ.get("REPO_HOSTS", "").split(",") if h.strip()
            )
            or DEFAULT_REPO_HOSTS,
            data_dir=Path(os.environ.get("DATA_DIR", "./data")).expanduser(),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            # parents[4] of apps/api/src/enigram/config.py: the repository root (/app in Docker).
            ts_graph_dir=Path(
                os.environ.get("TS_GRAPH_DIR", "").strip()
                or Path(__file__).resolve().parents[4] / "tools" / "ts-graph"
            ).expanduser(),
        )

    @property
    def judge(self) -> "Config":
        """This config with the judge as its chat LLM, for code that calls `cfg.llm_*`."""
        return replace(
            self,
            llm_provider=self.judge_provider,
            llm_base_url=self.judge_base_url,
            llm_api_key=self.judge_api_key,
            llm_model=self.judge_model,
            llm_max_tokens=self.judge_max_tokens,
        )

    @property
    def parsed_dir(self) -> Path:
        return self.data_dir / "parsed"

    @property
    def books_dir(self) -> Path:
        """Uploaded documents; the worker indexes this folder like `enigram index data/books`."""
        return self.data_dir / "books"

    @property
    def rag_dir(self) -> Path:
        return self.data_dir / "rag_storage"

    @property
    def index_location(self) -> str:
        """Where the index lives, for messages that tell the user what to delete."""
        if self.storage == "postgres":
            return (
                f"the LIGHTRAG_* rows of workspace {self.workspace!r} "
                f"in database {self.postgres_database!r}"
            )
        return str(self.rag_dir)

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def eval_dir(self) -> Path:
        return self.data_dir / "eval"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "index_manifest.json"
