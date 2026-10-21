# Plan: 001 scan-frame

## Architecture
```
static/index.html  --POST /scan (jpeg, question)-->  app/main.py (FastAPI)
                                                      ├─ app/gate.py     resize + phash dedupe + cache
                                                      ├─ app/budget.py   SQLite spend ledger + cap check
                                                      └─ app/agent.py    Pydantic AI agent → ScanResult
                                                               └─ app/schema.py
```

## Decisions
- **Pydantic AI** over LangGraph: the whole feature is "image in, validated object out"; typed `output_type` is the core abstraction. Revisit when workflows branch (feature 003).
- **Gemini Flash default, Haiku escalation**: Gemini free tier covers dev; Haiku is the paid fallback. Model is selected by `MODEL` env var, so switching is a config change.
- **SQLite ledger** instead of relying on provider dashboards: the cap must be enforceable offline and testable.
- **Perceptual hash (imagehash)** for dedupe: cheap, no ML dependency, good enough for "did the scene change".
- **Single-file PWA**: no build toolchain, works from the phone immediately via a tunnel.

## Risks
- Camera API needs HTTPS → use `cloudflared tunnel` or ngrok during dev.
- Vision bbox quality varies by model → bbox is optional in v1.
- Pydantic AI API surface changes between releases → pin version in requirements.
