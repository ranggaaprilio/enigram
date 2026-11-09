"""What the .env of this process sets, for the web UI's Setup page: one row per variable an admin
may need to look at. A secret is reported as set or not, never by value."""

from typing import Any

from enigram.config import NO_API_KEY, Config


def _row(group: str, label: str, env: str, value: str | None, is_set: bool) -> dict[str, Any]:
    return {
        "group": group,
        "label": label,
        "env": env,
        "value": value,
        "set": is_set,
        "secret": False,
    }


def _secret(group: str, label: str, env: str, value: str) -> dict[str, Any]:
    return {**_row(group, label, env, None, bool(value)), "secret": True}


def _cap(env: str, label: str, value: int) -> dict[str, Any]:
    return _row("Token caps", label, env, str(value) if value else "off", value > 0)


def setup_status(cfg: Config) -> dict[str, Any]:
    keys = len(cfg.api_keys)
    issuer = cfg.oidc.issuer if cfg.oidc else None
    poll = cfg.repo_poll_secs
    return {
        "workspace": cfg.workspace,
        "settings": [
            _row("Models", "Chat model", "LLM_MODEL", f"{cfg.llm_provider} {cfg.llm_model}", True),
            *(
                [_secret("Models", "DeepSeek API key", "DEEPSEEK_API_KEY", cfg.llm_api_key)]
                if cfg.llm_provider == "deepseek"
                else [
                    _row("Models", "Chat endpoint", "LLM_BASE_URL", cfg.llm_base_url, True),
                    # NO_API_KEY stands in for an unset key (local servers need none).
                    _secret(
                        "Models",
                        "Chat API key",
                        "LLM_API_KEY",
                        "" if cfg.llm_api_key == NO_API_KEY else cfg.llm_api_key,
                    ),
                ]
            ),
            _row(
                "Models",
                "Embeddings",
                "EMBED_MODEL",
                f"{cfg.embed_model}, {cfg.embed_dim} dimensions",
                True,
            ),
            _row(
                "Models",
                "Code embeddings",
                "CODE_EMBED_MODEL",
                f"{cfg.code_embed_model}, {cfg.code_embed_dim} dimensions",
                True,
            ),
            _secret("Models", "Voyage API key", "VOYAGE_API_KEY", cfg.voyage_api_key),
            _row(
                "Models",
                "Judge model",
                "JUDGE_MODEL",
                f"{cfg.judge_provider} {cfg.judge_model}",
                True,
            ),
            _row("Models", "Decision layer", "DECISION_PROVIDER", cfg.decision_provider, True),
            # Only Jev uses it; the judge alone (llm_judge) needs no key.
            *(
                [_secret("Models", "TypeSafe API key", "TYPESAFE_API_KEY", cfg.jev_api_key)]
                if cfg.decision_provider == "jev"
                else []
            ),
            _row(
                "Access",
                "API keys",
                "ENIGRAM_API_KEYS",
                (f"{keys} key" if keys == 1 else f"{keys} keys") if keys else None,
                keys > 0,
            ),
            _row("Access", "Sign-in provider", "OIDC_ISSUER", issuer, issuer is not None),
            _secret(
                "Repositories",
                "Private repository key",
                "REPO_CREDENTIALS_KEY",
                cfg.repo_credentials_key,
            ),
            _row(
                "Repositories",
                "Polling for new commits",
                "REPO_POLL_SECS",
                f"every {poll} s" if poll else "off",
                poll > 0,
            ),
            _row(
                "Repositories", "Hosts to clone from", "REPO_HOSTS", ", ".join(cfg.repo_hosts), True
            ),
            _cap("MAX_TOKENS_PER_JOB", "Per job", cfg.max_tokens_per_job),
            _cap("DAILY_TOKEN_BUDGET", "Per workspace per day", cfg.daily_token_budget),
            _cap("PER_KEY_DAILY_TOKENS", "Per key per day", cfg.per_key_daily_tokens),
        ],
    }
