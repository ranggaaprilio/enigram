"""Principals and roles (docs/rbac.md): importing ENIGRAM_API_KEYS, resolving a presented key to
a Caller, and the management actions behind `enigram principals`."""

import logging

from enigram.application.ports import PrincipalRepository
from enigram.config import Config
from enigram.domain.access import (
    Caller,
    Principal,
    check_principal_name,
    check_role,
    hash_key,
    new_api_key,
)
from enigram.domain.errors import EnigramError, Forbidden, NotFound

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
    check_principal_name(name)
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


async def set_default_workspace(principals: PrincipalRepository, name: str, workspace: str) -> None:
    """The workspace a key uses when a request names none; it must be a member there."""
    principal = await _named(principals, name)
    if not principal.owner and workspace not in await principals.roles(principal.id):
        raise EnigramError(f"{name} is no member of {workspace}; give it a role there first")
    await principals.set_default_workspace(principal.id, workspace)


async def set_owner(principals: PrincipalRepository, name: str, owner: bool) -> None:
    await principals.set_owner((await _named(principals, name)).id, owner)


async def revoke(principals: PrincipalRepository, name: str) -> None:
    await principals.revoke((await _named(principals, name)).id)


# The members page (docs/rbac.md, R3): an admin of the workspace manages who is in it.


async def members(principals: PrincipalRepository, workspace: str) -> list[dict[str, object]]:
    """The keys with a role in `workspace` (revoked ones left out), by name."""
    return [
        {
            "name": p.name,
            "kind": p.kind,
            "role": roles[workspace],
            "owner": p.owner,
            "display_name": p.display_name,
            "signed_in": p.signed_in,
        }
        for p, revoked, roles in await principals.list()
        if not revoked and workspace in roles
    ]


async def change_member(
    principals: PrincipalRepository, workspace: str, name: str, role: str | None
) -> None:
    """Give `name` a role in `workspace` (adding it), or None to remove it. The last admin
    cannot be lowered or removed, so a workspace always keeps someone who manages it."""
    if role is not None:
        check_role(role)
    principal = await _named(principals, name)
    current = (await principals.roles(principal.id)).get(workspace)
    if current == "admin" and role != "admin":
        admins = [m for m in await members(principals, workspace) if m["role"] == "admin"]
        if len(admins) <= 1:
            raise EnigramError(
                f"{name} is the last admin of {workspace}; make another member admin first"
            )
    if role is None:
        if not await principals.remove_member(workspace, principal.id):
            raise NotFound(f"{name} is no member of {workspace}")
        return
    await principals.set_role(workspace, principal.id, role)


async def revoke_key(
    principals: PrincipalRepository, caller: Caller, workspace: str, name: str
) -> None:
    """An admin may revoke a key of its workspace when it is also admin of every other workspace
    the key is in; an owner's key only an owner may revoke."""
    principal = await _named(principals, name)
    roles = await principals.roles(principal.id)
    if workspace not in roles and not principal.owner:
        raise NotFound(f"{name} is no member of {workspace}")
    if principal.owner and not (caller.principal is None or caller.principal.owner):
        raise Forbidden(f"Only an owner may revoke the owner key {name}")
    elsewhere = [w for w in roles if caller.role_in(w) != "admin"]
    if elsewhere:
        raise Forbidden(f"{name} is also in {', '.join(sorted(elsewhere))}, where you are no admin")
    if roles.get(workspace) == "admin":
        await change_member(principals, workspace, name, None)  # the last-admin check
    await principals.revoke(principal.id)
