# Feature 007: Ship it — Docker Compose, CI, observability

**Status**: Draft | **Created**: 2026-10-22

## Why
The agent works on one laptop with a hand-started server. A reviewer (or the owner on a
new machine, or the Raspberry Pi in 008) needs one command to run it, proof that every push
still passes the tests, and a way to see what a scan cost and how long each step took without
reading SQLite by hand. Three small, standard pieces; the learning value is in doing them
properly on a real agent: traces with tokens and cost per model call.

## User scenarios

### Primary
On a clean machine with Docker: `cp .env.example .env`, fill in the key, `docker compose up`.
The page is on http://localhost:8000, the ledger and notes live in a volume, restarts keep them.

### Secondary
The owner pushes a commit. GitHub Actions runs the test suite, the linter and a Docker build;
the README badge is green or red within a few minutes. A failing eval test (the offline
scorer) or a failing unit test blocks nothing (solo repo), but it is visible.

### Tertiary
The owner scans a desk, then runs `uv run python -m app.events tail` (or reads the log) and
sees one structured line per scan: request id, source, model(s), tokens in/out, cost,
latency per step (gate, primary, escalation, memory, lookup), cache hit, escalated, entities
linked, passages used. With `LOGFIRE_TOKEN` set, the same shows up as a trace in Logfire's
free tier, with the model calls as spans.

### Edge cases
- No `.env` → the container starts in demo mode (`MODEL=test`) and says so in the log.
- Volume missing → fresh ledger and empty memory; nothing else breaks.
- CI has no API keys → tests and the offline eval use the fake models only (already true).
- Tracing backend unreachable → scans are unaffected; events still go to the log.

## Functional requirements
- FR-1 **Dockerfile.** `python:3.12-slim`, dependencies installed with `uv sync --frozen --no-dev`,
  non-root user, `uvicorn app.main:app --host 0.0.0.0 --port 8000`. Image under ~300 MB.
- FR-2 **Compose.** `docker-compose.yml`: service `lens`, `env_file: .env` (optional), port 8000,
  a named volume mounted at `/data` holding the ledger (`DB_PATH=/data/lens.db`) and notes
  (`KB_DIR=/data/kb`), healthcheck on `/stats`, restart unless-stopped.
- FR-3 **CI.** `.github/workflows/ci.yml` on push and pull request: `uv sync`, `ruff check`,
  `pytest` (fake models, no secrets), `docker build`. Status badge in the README.
- FR-4 **Scan events.** Every `/scan` emits one JSON line (`event=scan`) to the app log with:
  request id, source, cached, model, primary model, escalated, tokens in/out per call, cost,
  total latency and per-step latency (gate, primary, escalation, memory, lookup), entity
  count, lookup count, status code. Cache hits and refusals (402/413/415/422) emit too.
- FR-5 **Tracing (optional).** With `LOGFIRE_TOKEN` set and `logfire` installed, Pydantic AI
  agents and the FastAPI app are instrumented; model calls appear as spans with token usage.
  Without it, nothing changes.
- FR-6 **Request id.** `/scan` returns `X-Request-Id` (the ledger request id) so a log line, a
  ledger row and a trace can be joined.

## Non-goals (this feature)
Kubernetes, cloud deployment, a metrics dashboard (the stats panel exists), alerting, log
shipping. The Pi image is 008.

## Acceptance
- `docker compose up` on this machine serves the page; a demo scan works from the container;
  the ledger survives `docker compose restart`.
- The CI workflow passes on GitHub for the commit that adds it.
- Tests: the event line contains the FR-4 fields for a paid scan, a cached scan and a 402;
  `X-Request-Id` is present; tracing setup is a no-op without the token.
