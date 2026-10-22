# Plan: 007 ship

## Pieces
```
Dockerfile, docker-compose.yml, .dockerignore          FR-1/2
.github/workflows/ci.yml, README badge                 FR-3
app/events.py   ScanEvent dataclass -> one JSON log line; timings collected in /scan   FR-4/6
app/tracing.py  configure() -> logfire if LOGFIRE_TOKEN else no-op                      FR-5
```

## Decisions
- **uv in the image, lockfile frozen.** The same resolver as development, reproducible, and
  `--no-dev` keeps pytest out of the image. Multi-stage is not worth it at this size.
- **One volume for state.** Ledger, memory and notes are all SQLite + Markdown under `/data`;
  one mount, one backup target. `KB_DIR` and `DB_PATH` already exist as settings.
- **CI with fake models only.** Secrets in CI would spend money on every push and leak risk for
  nothing; the eval scorer is covered by tests, the real eval is run by hand per the
  CLAUDE.md rule and its report is committed.
- **Events as log lines, not a new table.** The ledger already has the money; the event line
  adds timings and joins the pieces by request id. JSON lines are grep-able, ship anywhere,
  and cost nothing. A table would duplicate the ledger.
- **Logfire for traces, optional.** It is Pydantic AI's native instrumentation (one call), has
  a free tier, and shows model calls as spans with tokens. Behind a token so the default
  install has no network dependency besides the model providers.
- **ruff** added as a dev dependency: cheap, catches real mistakes, standard in CI.

## Risks
- Docker on the owner's laptop is slow to build; use the `uv` cache mount and a small base.
- Logfire's instrumentation API moves between Pydantic AI versions; keep the integration in
  one small module with a try/except and a test that it is a no-op without the token.
