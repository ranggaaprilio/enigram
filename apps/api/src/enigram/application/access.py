"""Principals and roles (docs/rbac.md): importing ENIGRAM_API_KEYS, resolving a presented key to
a Caller, and the management actions behind `enigram principals`."""

import logging

from enigram.application.ports import PrincipalRepository
from enigram.config import Config
from enigram.domain.access import Caller, Principal, check_role, hash_key, new_api_key
from enigram.domain.errors import EnigramError, NotFound

log = logging.getLogger(__name__)


async def import_env_keys(
    principals: PrincipalRepository, cfg: Config, workspaces: list[str]
) -> list[str]:
    """Run at API start. A key name seen for the first time becomes a principal: a key in
    ANNOTATION_VERIFIERS becomes `owner` and `admin` of every existing workspace, any other key
    `maintainer` of every existing workspace, so nothing that worked before stops working. A
    known name only gets its key hash updated (a rotated key), so narrowed roles stay narrowed
    and a revoked key stays revoked. Returns the names imported."""
    imported = []
    for name, key in cfg.api_keys:
        found = await principals.by_name(name)
        if found is not None:
            await principals.set_key_hash(found.id, hash_key(key))
            continue
        verifier = name in cfg.annotation_verifiers
        principal = await principals.create(
            name, "key", hash_key(key), cfg.workspace, owner=verifier
        )
        for workspace in workspaces:
            await principals.set_role(
                workspace, principal.id, "admin" if verifier else "maintainer"
            )
        imported.append(name)
    if imported:
        log.info("[access] imported keys from ENIGRAM_API_KEYS: %s", ", ".join(imported))
    return imported


async def caller_of(principals: PrincipalRepository, principal: Principal) -> Caller:
    return Caller(principal, await principals.roles(principal.id))


async def resolve_key(principals: PrincipalRepository, key: str) -> Caller | None:
    """None for an unknown or revoked key."""
    principal = await principals.by_key_hash(hash_key(key)) if key else None
    return await caller_of(principals, principal) if principal else None


async def _named(principals: PrincipalRepository, name: str) -> Principal:
    found = await principals.by_name(name)
    if found is None:
        raise NotFound(f"No principal {name!r}")
    return found


async def add_key(
    principals: PrincipalRepository, name: str, workspace: str, role: str = "viewer"
) -> str:
    """A new key, member of `workspace` with `role`; the key itself is returned once and only its
    hash is kept."""
    check_role(role)
    key = new_api_key()
    principal = await principals.create(name, "key", hash_key(key), workspace)
    await principals.set_role(workspace, principal.id, role)
    return key


async def set_role(
    principals: PrincipalRepository, name: str, workspace: str, role: str | None
) -> None:
    """`role` None removes the membership."""
    principal = await _named(principals, name)
    if role is None:
        if not await principals.remove_member(workspace, principal.id):
            raise EnigramError(f"{name} is no member of {workspace}")
        return
    await principals.set_role(workspace, principal.id, role)


async def set_owner(principals: PrincipalRepository, name: str, owner: bool) -> None:
    await principals.set_owner((await _named(principals, name)).id, owner)


async def revoke(principals: PrincipalRepository, name: str) -> None:
    await principals.revoke((await _named(principals, name)).id)
