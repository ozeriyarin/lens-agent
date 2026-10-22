# lens-agent (feature 007). Small, reproducible, non-root.
#   docker compose up          -> http://localhost:8000
# The lockfile is installed frozen with uv, dev dependencies left out.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYDANTIC_AI_NO_BANNER=1 \
    DB_PATH=/data/lens.db \
    KB_DIR=/data/kb

WORKDIR /app

# Dependencies first (cached unless the lockfile changes), then the code.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY static ./static
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Only /data is written at runtime; chown-ing /app would copy the whole venv into a new
# layer (it doubled the image: 395 MB twice). The code and venv stay root-owned, world-readable.
RUN useradd --create-home --uid 1000 lens \
    && mkdir -p /data/kb && chown -R lens:lens /data
USER lens
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3).status == 200 else 1)"]

CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
