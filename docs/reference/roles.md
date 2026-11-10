---
title: Roles and permissions
sidebar_label: Roles and permissions
description: The minimum role for every HTTP route and MCP tool.
---

# Roles and permissions

Roles are per workspace and cumulative: each role has every right of the roles listed before it.

| Role | Scope | Meant for |
|---|---|---|
| `viewer` | per workspace | Readers: documents, Chat, Trace, Knowledge |
| `contributor` | per workspace | PMs, QA and agents: note proposals, ✓ *Looks right*, memory |
| `maintainer` | per workspace | Developers: verify notes, documents, repositories, graph, conflicts |
| `admin` | per workspace | Workspace owner: members, keys, custom agents, private repository tokens, webhook secrets |
| `owner` | global | Creates workspaces; `admin` in every workspace |

A new member gets `viewer`. Setting up keys and sign-in: [Access control](../self-hosting/access-control.md).

## General rules

- **Status codes.** No valid key → **401**. Not a member of the workspace → **404**, so a
  workspace's existence does not leak. A member with too low a role → **403**, naming the role
  needed.
- **Workspace.** The `X-Enigram-Workspace` header; without it, the principal's default workspace.
- **Local mode.** Without `ENIGRAM_API_KEYS` the API is open and roles are off.
- **MCP over stdio** has no roles: it uses `WORKSPACE` from the environment, for the owner on
  their own machine.
- **Ownership.** Some actions are allowed to their author below the minimum role ("author"
  below). The author must still be at least a `contributor`; an author demoted to `viewer` loses
  that right.
- **CLI.** `enigram principals ...` works on the database directly and counts as `owner`.

## HTTP routes

### Open

| Route | Access |
|---|---|
| `GET /health` | open |
| `GET /auth/config`, `/auth/login`, `/auth/callback` | open |
| `POST /repos/{id}/webhook` | HMAC signature with the repository's secret, not a Bearer key |

### Workspaces and principals

| Route | Minimum |
|---|---|
| `GET /workspaces`, `GET /me` | any valid key |
| `POST /workspaces` | `owner` (becomes `admin` of the new workspace) |
| `GET /members`, `POST /members`, `PUT /members/{name}`, `DELETE /members/{name}` | `admin` (the last admin cannot be demoted or removed) |
| `POST /keys` | `admin` |
| `DELETE /keys/{name}` | `admin` of every workspace the key is in; an owner's key only by an `owner` |
| `GET /setup` | `owner` |

### Documents, jobs and the Dashboard

| Route | Minimum |
|---|---|
| `GET /documents`, `GET /jobs/{id}` | `viewer` |
| `POST /documents`, `DELETE /documents/{book}` | `maintainer` |
| `GET /stats`, `GET /stats/repos`, `GET /stats/repos/{id}` | `viewer` (`tokens.by_key` only for `admin`) |
| `GET /stats/workspaces` | `owner` |

### Repositories and code

| Route | Minimum |
|---|---|
| `GET /repos`, `GET /repos/{id}/symbols`, `/features`, `/tree`, `/symbols/{symbol}`, `GET /targets` | `viewer` |
| `POST /repos` without `token`, `DELETE /repos/{id}` | `maintainer` |
| `POST /repos` with `token` | `admin` |
| `POST /repos/{id}/secret` | `admin` |
| `POST /repos/{id}/symbols/{symbol}/review` | `contributor` |
| `DELETE /repos/{id}/symbols/{symbol}/review` | `contributor` (its own ✓) / `maintainer` (anyone's) |

### Notes

| Route | Minimum |
|---|---|
| `GET /annotations`, `GET /annotations/{id}`, `GET /review` | `viewer` |
| `POST /annotations` | `contributor` (always `proposed`); a `maintainer`'s is `verified` unless `proposed: true` |
| `DELETE /annotations/{id}` | `contributor` (author) / `maintainer` (anyone's) |
| `PATCH /annotations/{id}` | per field, below |

| `PATCH` field | Minimum |
|---|---|
| `text`, `by` | `contributor` (author) / `maintainer`. A contributor's edit makes the note `proposed` again |
| `status: needs_verification` | `contributor` |
| `status: verified` / `rejected` | `maintainer`; an agent's (`via = mcp`) proposal never by the key that proposed it |
| `owner`, `verify_by`, `conflict`, `target_ref` (relink) | `maintainer` |

### Questions, conflicts and the graph

| Route | Minimum |
|---|---|
| `POST /query`, `GET /queries`, `GET /queries/{id}` | `viewer` (traces show every key's questions in the workspace) |
| `GET /conflicts` | `viewer` |
| `POST /conflicts/detect` | `maintainer` |
| `GET /graph/search`, `/graph/relations`, `/graph/types` | `viewer` |
| `POST /graph/entity/edit`, `/relation/edit`, `/entity/merge`, `DELETE /graph/entity/{name}`, `DELETE /graph/relation` | `maintainer` |

### Memory

| Route | Minimum |
|---|---|
| `GET /memories`, `GET /memories/{id}`, `DELETE /memories/{id}` | `contributor` (its own) / `maintainer` (every key's) |
| `POST /memories/{id}/note` | `contributor` (becomes a proposal) |

### Chat

| Route | Minimum |
|---|---|
| `GET /chat/agents`, `GET`/`POST /chat/sessions`, `GET`/`PATCH /chat/sessions/{id}`, `POST /chat/sessions/{id}/messages` | `viewer`. Only the session's owner writes in it, renames or shares it; another person's session is readable only when shared, else 404 |
| `DELETE /chat/sessions/{id}` | The owner, or `admin` (anyone's) |
| `POST /chat/agents`, `PATCH`/`DELETE /chat/agents/{name}` | `admin` (built-in agents cannot change) |

## MCP tools (HTTP)

The workspace is the connection's (`/mcp/w/<ws>` or the header), or the tool's `workspace`
argument; both are checked against the same membership as the API.

| Tool | Minimum |
|---|---|
| `list_workspaces` | any valid key |
| `query_knowledge_base`, `list_documents`, `what_changed_since`, `get_annotations`, `get_conflicts` | `viewer` |
| `add_memory`, `search_memory` | `contributor` (its own memories only) |
| `delete_memory` | `contributor` (its own) / `maintainer` (other keys') |
| `propose_annotation` | `contributor` (always `proposed`) |

## Sign-in sessions

- OpenID Connect (authorization code + PKCE). The ID token is fully checked: JWKS signature,
  issuer, audience, expiry, nonce; the email must be in `OIDC_ALLOWED_DOMAINS`.
- The first sign-in creates a principal of kind `user` named by the email, a `viewer` of the
  process workspace (or the role an admin invited it with). A name that clashes with a key, or a
  revoked user, is refused.
- The session cookie `enigram_session` is HttpOnly, SameSite=Lax, and Secure when the redirect URL
  is https. Its hash is in the `sessions` table for `SESSION_DAYS` days. Writes through the cookie
  need the `X-Requested-With` header.
- `by` on notes and reviews is the person's display name from the provider; nobody else can set
  it.
