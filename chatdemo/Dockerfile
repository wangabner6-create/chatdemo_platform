# Built on top of the uv-managed project (see design note D11). Two stages: the
# first uses uv to populate a virtualenv, the second ships only that venv plus
# the source tree.
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS build

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Resolve and install dependencies before copying the project itself, so this
# layer stays cached across source-only changes.
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev || uv sync --no-install-project --no-dev

# pyproject's `readme` field points at README.md, so the build step needs it present.
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev

FROM python:3.11-slim-bookworm AS runtime
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["chatdemo"]
