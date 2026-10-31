# One image for `enigram api` and `enigram worker` (see docker-compose.yml). Built from the
# repository root, the uv workspace: `docker build -f apps/api/Dockerfile .`
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

# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/README.md ./apps/api/
RUN uv sync --frozen --no-dev --package enigram --no-install-workspace
COPY apps/api/src ./apps/api/src
RUN uv sync --frozen --no-dev --package enigram

ENV PATH="/app/.venv/bin:$PATH"
