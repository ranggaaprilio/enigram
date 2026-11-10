---
title: Third-party licenses
description: Audit of the licenses of Enigram's runtime dependencies.
---

# Third-party licenses

Audit of the runtime dependencies, done 2026-11-05 against `uv.lock`, `apps/web/package-lock.json`
and `tools/ts-graph/package-lock.json`. Development tools (ruff, pytest, vitest, ...) are not
distributed and are not counted.

## Method

Run from the repository root:

```bash
uv export --frozen --no-dev --package enigram --no-emit-project --no-hashes -q > "$TMPDIR/req.txt"
uv run --isolated --no-project --with pip-licenses --with-requirements "$TMPDIR/req.txt" \
  pip-licenses --from=mixed --format=json
(cd apps/web && npx --yes license-checker-rseidelsohn --production --summary)
(cd tools/ts-graph && npx --yes license-checker-rseidelsohn --production --summary)
```

Without `--isolated`, `uv run` layers on the project `.venv` and also lists the dev tools. Read the
short license name: some packages (ragas, tiktoken) put the whole license text in the field.

## Summary

| Part | Packages | Licenses |
|---|---|---|
| Python (`apps/api`), macOS resolution | 182 | MIT 91, BSD 40, Apache-2.0 34, dual/multi permissive 10, PSF 3, ISC 1, MPL-2.0 (with MIT/Apache) 3 |
| Web UI (`apps/web`) | 110 | MIT 109, ISC 1 (2026-11-05, before the shadcn/ui restyle; see Findings) |
| Sidecar (`tools/ts-graph`) | 10 | MIT 9, BlueOak-1.0.0 1 (`minimatch`) |

`license-checker` also lists the project's own package as `UNLICENSED`, because it is marked
`"private": true`; its `license` field is `AGPL-3.0-only`. A few Python packages are
Windows- or Emscripten-only (`colorama`, `pywin32`, `tzdata`, `httpx2-jsfetch`) and are not
installed in the Linux image.

## Findings

- No GPL, LGPL, AGPL, SSPL or BUSL dependency.
- MPL-2.0 (file-level copyleft): `certifi`, `orjson`, `tqdm`. Used unmodified, so compatible with
  AGPL-3.0; modified MPL files would have to stay under MPL.
- BlueOak-1.0.0 (`minimatch`, via ts-morph) is a permissive license.
- The web UI restyle (2026-11-06) added only permissive licenses, read from the `license` fields of
  the non-dev entries in `apps/web/package-lock.json`: `class-variance-authority` (Apache-2.0),
  `lucide-react` (ISC), `tslib` (0BSD), the rest MIT. The bundled Inter font
  (`@fontsource-variable/inter`) is OFL-1.1: it may be bundled and served with the UI, and the font
  files themselves stay under OFL.
- Docker base images: `python:3.12-slim`, `node:22-bookworm-slim`, `node:22-alpine` (build stage
  only), `nginx:1.29-alpine`, and `pgvector/pgvector:pg17` (PostgreSQL License) for the database.
- Docling downloads its layout model weights from Hugging Face on the first PDF. They are not part
  of the repository or the image and keep their own licenses.
- The external APIs (DeepSeek, Voyage, TypeSafe) are services, not dependencies; their own terms
  apply to what you send them (see the README, "Where your data goes").

All runtime dependencies are compatible with distributing Enigram under AGPL-3.0-only.
