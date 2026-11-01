# One image for `enigram api` and `enigram worker` (see docker-compose.yml). Built from the
# repository root, the uv workspace: `docker build -f apps/api/Dockerfile .`

# The ts-morph sidecar (tools/ts-graph) runs on Node; only the binary and the sidecar's own
# node_modules are copied, both built on bookworm like python:3.12-slim.
FROM node:22-bookworm-slim AS tsgraph
WORKDIR /tsgraph
COPY tools/ts-graph/package.json tools/ts-graph/package-lock.json ./
RUN npm ci --omit=dev
COPY tools/ts-graph/ ./

FROM python:3.12-slim

# Docling's PDF backends need libgl1/libglib. Docling renders each page for its layout model, and
# without system fonts a PDF's non-embedded fonts (Helvetica, Times, ...) come out as empty boxes:
# the model then misses headings such as "References", so back matter is no longer dropped.
# Liberation has metric-compatible Helvetica/Times/Courier substitutes. git: index_repo clones
# public repositories.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 fontconfig fonts-liberation \
       git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Config's TS_GRAPH_DIR default resolves to /app/tools/ts-graph.
COPY --from=tsgraph /usr/local/bin/node /usr/local/bin/node
COPY --from=tsgraph /tsgraph /app/tools/ts-graph

# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/README.md ./apps/api/
RUN uv sync --frozen --no-dev --package enigram --no-install-workspace
COPY apps/api/src ./apps/api/src
RUN uv sync --frozen --no-dev --package enigram

ENV PATH="/app/.venv/bin:$PATH"
