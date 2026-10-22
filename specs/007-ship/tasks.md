# Tasks: 007 ship

- [x] T1 Dockerfile, .dockerignore, docker-compose.yml with volume + healthcheck; build, run, demo scan from the container, restart keeps the ledger
- [x] T2 ruff config + fixes; `.github/workflows/ci.yml` (uv sync, ruff, pytest, docker build); README badge; first green run on GitHub
- [x] T3 Scan events: `app/events.py`, timings in `/scan`, `X-Request-Id`; tests for paid / cached / refused
- [x] T4 Tracing: `app/tracing.py` with Logfire behind `LOGFIRE_TOKEN`; no-op test; README observability section; close the spec
