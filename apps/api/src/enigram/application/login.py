"""Per-person login for the web UI (docs/rbac.md, R4): who may sign in, the principal a person
becomes, and the web sessions that replace the shared ENIGRAM_WEB_KEY."""

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from enigram.application.access import caller_of
from enigram.application.ports import PrincipalRepository
from enigram.config import Config, OidcConfig
from enigram.domain.access import Caller, Identity, Principal, hash_key, new_api_key
from enigram.domain.errors import Forbidden

log = logging.getLogger(__name__)

SECRET_BYTES = 32


@dataclass(frozen=True)
class LoginStart:
    """Kept in a short-lived cookie between the redirect to the provider and its callback."""

    state: str  # ties the callback to this browser (no login CSRF)
    nonce: str  # ties the ID token to this sign-in
    verifier: str  # PKCE: the code is useless without it


def new_login() -> LoginStart:
    return LoginStart(*(secrets.token_urlsafe(SECRET_BYTES) for _ in range(3)))


def allowed(oidc: OidcConfig, email: str) -> bool:
    domain = email.rpartition("@")[2]
    return "*" in oidc.allowed_domains or domain in oidc.allowed_domains


async def sign_in(principals: PrincipalRepository, cfg: Config, who: Identity) -> Principal:
    """The user principal of `who`. The first sign-in creates it as a `viewer` of the process
    workspace (new members are viewers, docs/rbac.md); an admin raises the role on Members."""
    assert cfg.oidc is not None
    if not allowed(cfg.oidc, who.email):
        raise Forbidden(f"{who.email} may not sign in here (OIDC_ALLOWED_DOMAINS)")
    found = await principals.by_name(who.email)
    if found is None:
        found = await principals.create(who.email, "user", None, cfg.workspace)
        await principals.set_role(cfg.workspace, found.id, "viewer")
        log.info("[access] new user %s, viewer of %s", who.email, cfg.workspace)
    elif found.kind != "user":
        raise Forbidden(f"{who.email} is the name of an API key, not a person")
    elif await principals.is_revoked(found.id):
        raise Forbidden(f"{who.email} was revoked; ask an admin")
    if who.name != found.display_name:
        await principals.set_display_name(found.id, who.name)
    return found


async def start_session(principals: PrincipalRepository, principal: Principal, days: int) -> str:
    """A new session token for the cookie; only its hash is stored."""
    token = new_api_key()
    expires = datetime.now(UTC) + timedelta(days=days)
    await principals.create_session(principal.id, hash_key(token), expires)
    return token


async def session_caller(principals: PrincipalRepository, token: str) -> Caller | None:
    """None for an unknown, expired or revoked session."""
    principal = await principals.by_session(hash_key(token)) if token else None
    return await caller_of(principals, principal) if principal else None


async def end_session(principals: PrincipalRepository, token: str) -> None:
    if token:
        await principals.delete_session(hash_key(token))
