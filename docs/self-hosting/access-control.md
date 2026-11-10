---
title: Access control
description: API keys, roles per workspace, managing keys from the CLI, and sign-in with OpenID Connect.
---

# Access control

There are two kinds of principal:

| Kind | Used by | Set up with |
|---|---|---|
| **API key** | Agents (MCP), scripts, the shared web UI key | `ENIGRAM_API_KEYS`, `enigram principals`, or **Settings → Members** |
| **Person** | People signing in to the web UI | An OpenID Connect provider ([Sign-in](#sign-in)) |

Each principal has one [role](#roles) per workspace.

## API keys

Set named keys in `.env`; the name is what query logs record, the key is never stored:

```bash title=".env"
ENIGRAM_API_KEYS="laptop-a:$(openssl rand -hex 24),web:$(openssl rand -hex 24)"
ENIGRAM_WEB_KEY=<the web key>   # nginx sends it for the web UI
API_BIND=0.0.0.0                # publish the API port on the LAN, e.g. for MCP over HTTP
```

With keys set, every route except `GET /health` and the signed repository webhook needs
`Authorization: Bearer <key>`; otherwise it answers 401.

```bash
curl -H "Authorization: Bearer $KEY" localhost:8080/me
```

:::danger

Without `ENIGRAM_API_KEYS` every route is open (no roles) and the API logs a warning. Keep
`API_BIND` at its default (`127.0.0.1`) until keys are set. The API speaks plain HTTP: on an
untrusted network put TLS in front (a reverse proxy or Tailscale).

:::

## Roles

Each role has every right of the ones before it:

| Role | May |
|---|---|
| `viewer` | Read every page, ask (`/query`, MCP `query_knowledge_base`), see traces |
| `contributor` | + propose notes, ✓ *Looks right*, flag a note for re-check, edit or delete its own notes, agent memory, MCP `propose_annotation` |
| `maintainer` | + verify or reject notes, upload and delete documents, index and delete repositories, edit the graph, detect conflicts |
| `admin` | + members and API keys, custom chat agents, webhook secrets, private repository tokens |

An **`owner`** is `admin` in every workspace and the only one who may create workspaces.

- A workspace the principal is no member of answers 404, as one that does not exist; a role too
  low answers 403.
- `GET /me` shows the principal's name and its role per workspace; `GET /workspaces` lists only
  its workspaces.
- Without `X-Enigram-Workspace` a request goes to the principal's default workspace.

Every route and MCP tool with its minimum role: [Roles and permissions](../reference/roles.md).

### First start

At API start every key in `ENIGRAM_API_KEYS` seen for the first time becomes a principal:

- a key in `ANNOTATION_VERIFIERS` becomes `owner` (so `web` there keeps the UI's rights, *New
  workspace* included);
- any other key becomes `maintainer` of every existing workspace.

Later starts only update a rotated key; roles set since then are kept.

## Manage keys and roles

Without a restart, from the CLI:

```bash
uv run enigram principals list
uv run enigram principals add-key qa-team --workspace whiteboard --role contributor  # prints the key once
uv run enigram principals role laptop-b default viewer      # or `none` to remove the membership
uv run enigram principals default laptop-b whiteboard        # used when a request names none
uv run enigram principals owner laptop-b [--off]
uv run enigram principals revoke qa-team                     # 401 from the next request on
```

Or in the web UI, as an admin: **Settings → Members → API keys** makes a key (shown once), changes
its role, removes it or revokes it, per workspace. A key already in another workspace is added
with `enigram principals role`.

- Keys are stored as SHA-256 hashes (table `principals`, roles in `workspace_members`).
- A key can be a member of several workspaces, with one role in each. Its default workspace is
  the one of `add-key` until `default` changes it, and must be one of its workspaces.
- MCP over HTTP checks the same roles in the workspace each tool call uses; MCP over stdio has
  none.
- Without sign-in, everyone in the web UI shares the `web` key and its role.

## Sign-in

With an OpenID Connect provider, each person signs in to the web UI with their work account
instead of sharing the `web` key. API keys keep working beside sign-in, for agents and scripts.

### Set up

1. Register a web application at the provider (Google Cloud console, Microsoft Entra *App
   registrations*, Keycloak, ...) with the redirect URL `https://<web host>/api/auth/callback`,
   and put its issuer, client id and secret in `.env` (`OIDC_*` in `.env.example`). For Entra use
   the tenant's issuer (`https://login.microsoftonline.com/<tenant id>/v2.0`), not `common`.
2. Set `OIDC_ALLOWED_DOMAINS` to the email domains that may sign in (`*` for every account of a
   provider that is already one organisation's).

   :::warning

   It is required: with Google, any Google account could otherwise sign in.

   :::

3. Leave `ENIGRAM_WEB_KEY` empty and set `WEB_HOST`, `WEB_ORIGIN` (and `WEB_BIND=0.0.0.0` to
   publish the port, ideally behind TLS: the session cookie is `Secure` when the redirect URL is
   https).

### People and roles

- The first sign-in makes the person a principal of kind `user` named by their email, a `viewer`
  of `WORKSPACE`.
- To give someone a role before that, an admin invites their email on **Settings → Members →
  People**. The person shows as *invited* until they sign in, then gets that role. No email is
  sent; the admin sends the workspace link.
- Make the first owner after their first sign-in with
  `uv run enigram principals owner <email>`.
- A session lasts `SESSION_DAYS` (7) and ends at *Sign out* in the account menu or when the
  person is revoked.
- Notes and ✓ reviews carry the person's name from the provider, not a typed one.

### Try it locally with Keycloak

The `keycloak` compose profile runs a development Keycloak with a realm `enigram`
([`tools/keycloak/enigram-realm.json`](https://github.com/ranggaaprilio/enigram/blob/main/tools/keycloak/enigram-realm.json)):
the client `enigram-web` and three users whose password is their name:

| User | Email domain | Signs in |
|---|---|---|
| `alice` | `example.com` | yes |
| `bob` | `example.com` | yes |
| `eve` | `other.test` | no, refused by the domain rule |

```bash
docker compose --profile keycloak up -d keycloak   # admin console: http://keycloak.localhost:8081 (admin/admin)
```

In `.env`, empty `ENIGRAM_WEB_KEY` and set:

```bash title=".env"
OIDC_ISSUER=http://keycloak.localhost:8081/realms/enigram
OIDC_CLIENT_ID=enigram-web
OIDC_CLIENT_SECRET=local-dev-secret-not-for-production
OIDC_REDIRECT_URL=http://localhost:3000/api/auth/callback
OIDC_ALLOWED_DOMAINS=example.com
```

Then `docker compose up -d api web`, open http://localhost:3000 and sign in as `alice`.
`docker compose exec api enigram principals owner alice@example.com` makes that user the owner.

The issuer `keycloak.localhost` is one name for both sides: browsers resolve `*.localhost` to this
machine (Chrome, Edge and Firefox; not Safari) and the API container reaches Keycloak through a
network alias, so the token's issuer matches `OIDC_ISSUER`. For `make web-dev` use the redirect
URL on port 5173 instead.

:::danger

The passwords and client secret are fixed: never publish this Keycloak.

:::
