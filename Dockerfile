# lens-agent (feature 007). Small, reproducible, non-root.
#   docker compose up          -> http://localhost:8000
# Two stages: the venv is built with uv, then only the venv and the code are copied into a
# clean base, so the uv binary and build leftovers are not shipped.
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first (cached unless the lockfile changes), then the project itself.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYDANTIC_AI_NO_BANNER=1 \
    DB_PATH=/data/lens.db \
    KB_DIR=/data/kb \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY app ./app
COPY static ./static

# Only /data is written at runtime; the code and venv stay root-owned, world-readable.
RUN useradd --create-home --uid 1000 lens \
    && mkdir -p /data/kb && chown -R lens:lens /data
USER lens
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
