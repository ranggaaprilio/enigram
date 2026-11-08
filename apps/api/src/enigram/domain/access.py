"""Who may do what (docs/rbac.md): a principal (an API key, later a logged-in user) holds one
role per workspace; roles are ordered, each with every right of the ones below. An `owner` is
global and acts as `admin` in every workspace."""

import hashlib
import re
import secrets
from dataclasses import dataclass, field

from enigram.domain.errors import EnigramError

ROLES = ("viewer", "contributor", "maintainer", "admin")
PRINCIPAL_KINDS = ("key", "user")
# Bytes of randomness in a new API key (token_urlsafe: about 43 characters).
KEY_BYTES = 32
# A new key's name: shown in logs, notes and budgets (keys from ENIGRAM_API_KEYS keep theirs).
PRINCIPAL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def check_role(role: str) -> str:
    if role not in ROLES:
        raise EnigramError(f"role must be one of {', '.join(ROLES)}; got {role!r}")
    return role


def check_principal_name(name: str) -> str:
    if not PRINCIPAL_NAME.match(name):
        raise EnigramError(
            "a key name is 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit"
        )
    return name


def at_least(role: str | None, minimum: str) -> bool:
    return role is not None and ROLES.index(role) >= ROLES.index(minimum)


def hash_key(key: str) -> str:
    """Keys are random, so a plain SHA-256 is enough to store and look them up."""
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key() -> str:
    return secrets.token_urlsafe(KEY_BYTES)


@dataclass(frozen=True)
class Principal:
    id: int
    name: str  # the actor name in logs, notes and budgets, as the API key name was
    kind: str
    owner: bool
    default_workspace: str | None
    display_name: str | None = None  # a signed-in user's name from the provider


@dataclass(frozen=True)
class Identity:
    """A person the login provider vouched for (an ID token's checked claims)."""

    email: str  # lowercase; a user principal's name
    name: str | None


@dataclass(frozen=True)
class Caller:
    """The principal of one request and its roles per workspace. `principal` None means access
    control is off (no ENIGRAM_API_KEYS, or MCP over stdio): every caller acts as admin."""

    principal: Principal | None
    roles: dict[str, str] = field(default_factory=dict)

    def role_in(self, workspace: str) -> str | None:
        """None when the caller is no member of the workspace."""
        if self.principal is None or self.principal.owner:
            return "admin"
        return self.roles.get(workspace)

    def workspace_for(self, asked: str | None, process: str) -> str:
        """The workspace a request uses: the one it asks for (header, MCP path or tool
        argument), else the principal's default, else the process workspace."""
        default = self.principal.default_workspace if self.principal else None
        return asked or default or process

    def workspaces(self, existing: list[str]) -> list[str]:
        return [w for w in existing if self.role_in(w) is not None]


OPEN = Caller(None)
