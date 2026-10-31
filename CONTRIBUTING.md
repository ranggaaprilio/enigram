# Contributing to Enigram

Thanks for considering a contribution. Enigram is licensed under the GNU AGPL-3.0 (see
`LICENSE`). This page covers what you need to know before opening a pull request.

## Contributor License Agreement (required)

Before your first pull request can be merged you must agree to the Enigram Individual
Contributor License Agreement in [`CLA.md`](CLA.md). You keep the copyright on your work; the
CLA gives the project owner a perpetual license to it, including the right to distribute it
under other terms. That is what lets the project offer licenses other than AGPL-3.0 in the
future without having to track down every contributor. The CLA tool asks you to agree once, on
your first pull request; it blocks merging until you do.

If you contribute on behalf of your employer, make sure your employer allows it (CLA section 4).

## Before You Start

- **Open an issue first** for anything beyond a small fix, so the design can be discussed before
  code is written. The product plans live in `.claude/PRPs/prds/`; a change should fit one of
  them or say why it does not.
- Check the open issues and pull requests to avoid duplicate work.

## Development Setup

Prerequisites: [uv](https://docs.astral.sh/uv/), Node 22.22 or newer (`web/.node-version`),
Docker (for Postgres), and a chat + embedding model reachable over an OpenAI-compatible API
(see the README for oMLX and DeepSeek).

```bash
uv sync
cp .env.example .env            # fill in the model settings
docker compose up -d postgres
uv run enigram check
cd web && npm install
uv run pre-commit install       # git hooks: ruff, tsc, commit-message lint
```

## Checks That Must Pass

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest -m "not omlx and not docling"   # fast unit tests
cd web && npm test && npm run build
```

Run the slower integration tests when your change touches parsing or the LLM path:
`uv run pytest` (needs the Docling models and, for the `omlx` tests, a running model server).

## Pull Requests

- One change per pull request; keep refactors separate from behavior changes.
- Add or update tests for what you change. New behavior without a test will be asked for one.
- Match the surrounding code: comment density, naming, and the existing style. Do not
  reformat or "improve" code you did not need to touch.
- Update the README when you change a command, an endpoint, an environment variable, or an MCP
  tool.
- Describe *why* in the pull request, not only *what*; link the issue.
- Commit messages follow the existing history: `feat:`, `fix:`, `docs:`, `test:`, with an
  optional scope such as `fix(web): …`. The `commit-msg` hook rejects anything else.

## Reporting Security Issues

Do not open a public issue for a vulnerability. Email [SECURITY CONTACT EMAIL] with the details
and give us a reasonable time to respond before any disclosure.

## Trademarks

"Enigram" and its logo are not covered by the AGPL-3.0 license. See `TRADEMARK.md` for what
you may and may not do with the name.

## Code of Conduct

Be respectful and constructive. Maintainers may remove comments, commits, issues, and
contributors that do not meet that bar.
