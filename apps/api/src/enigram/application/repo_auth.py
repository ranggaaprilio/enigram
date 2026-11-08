"""Access tokens for private repositories: one personal access token per repository URL and
workspace, encrypted with REPO_CREDENTIALS_KEY (Fernet) before it reaches the database.

git gets the token as an HTTP Authorization header through GIT_CONFIG_* environment variables,
so it never appears in a URL, a command line, a job payload or a git config file on disk."""

import base64

from cryptography.fernet import Fernet, InvalidToken

from enigram.application.ports import CodeGraphRepository
from enigram.config import Config
from enigram.domain.errors import EnigramError

TOKEN_MAX_CHARS = 512
# GitHub and GitLab accept a personal access token as the password with any user name.
_GIT_USER = "x-access-token"
REDACTED = "***"


def new_key() -> str:
    """A fresh REPO_CREDENTIALS_KEY."""
    return Fernet.generate_key().decode()


def _fernet(cfg: Config) -> Fernet:
    if not cfg.repo_credentials_key:
        raise EnigramError(
            "Private repositories need REPO_CREDENTIALS_KEY (make one with `enigram repo-key`)"
        )
    try:
        return Fernet(cfg.repo_credentials_key.encode())
    except ValueError as e:
        raise EnigramError("REPO_CREDENTIALS_KEY is not a valid key") from e


def check_token(token: str) -> str:
    """The token without surrounding spaces; refused when it could break the header."""
    token = token.strip()
    if not token:
        raise EnigramError("The access token is empty")
    if len(token) > TOKEN_MAX_CHARS or not token.isprintable() or any(c.isspace() for c in token):
        raise EnigramError("Not an access token")
    return token


def encrypt_token(cfg: Config, token: str) -> str:
    return _fernet(cfg).encrypt(check_token(token).encode()).decode()


def decrypt_token(cfg: Config, sealed: str) -> str:
    try:
        return _fernet(cfg).decrypt(sealed.encode()).decode()
    except InvalidToken as e:
        raise EnigramError(
            "The stored access token cannot be read with this REPO_CREDENTIALS_KEY; "
            "add the repository again with its token"
        ) from e


def _basic(token: str) -> str:
    return base64.b64encode(f"{_GIT_USER}:{token}".encode()).decode()


def auth_env(token: str | None) -> dict[str, str]:
    """GIT_CONFIG_* variables that send the token as HTTP basic auth; empty without one."""
    if not token:
        return {}
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {_basic(token)}",
    }


def scrub(text: str, token: str | None) -> str:
    """`text` with the token (and its basic-auth form) replaced, for errors and logs."""
    if not token:
        return text
    return text.replace(_basic(token), REDACTED).replace(token, REDACTED)


async def token_for(cfg: Config, graph: CodeGraphRepository | None, url: str) -> str | None:
    """The decrypted access token of a repository URL; None for a public one."""
    sealed = await graph.repo_token(url) if graph else None
    return decrypt_token(cfg, sealed) if sealed else None


async def save_token(cfg: Config, graph: CodeGraphRepository, url: str, token: str) -> None:
    await graph.set_repo_token(url, encrypt_token(cfg, token))
