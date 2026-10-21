# lens-agent — project context for Claude Code

This file is the handoff from the planning conversation (claude.ai, 2026-10-21). Read it fully before doing anything.

## What this project is
A portfolio AI-engineering project: a **multimodal vision agent**. The phone camera (later a Raspberry Pi) sends a frame to a server; an agent built with **Pydantic AI** returns a **typed list of detected objects** (label, category, position, confidence, optional bbox; for people an estimated mood and age range), can answer a question about the scene, remembers entities it has seen, and later uses RAG and evals. Built **spec-first with GitHub Spec Kit**.

Goal for the owner (Yarin): learn core AI-engineering concepts hands-on (agents, tool use, structured outputs, RAG, memory, evals, observability, cost control, edge-to-cloud) and have something concrete to discuss in full-stack / AI-engineer interviews.

## Non-negotiables (also in .specify/memory/constitution.md)
1. Typed outputs everywhere (Pydantic models, never free text).
2. **Hard budget cap: 5 USD/month**, enforced in code before every paid call, logged to SQLite, mirrored by a provider-side spend limit.
3. Pay only for new information: downscale to ≤1024px, perceptual-hash dedupe, cheapest model first (Gemini Flash free tier), escalate to Claude Haiku only on low confidence or follow-up.
4. Privacy: never store raw frames; memory holds descriptions and embeddings only; age/mood are estimates with confidence.
5. Tests for every tool and gate; an eval set for agent quality.
6. Spec before code; if code drifts, update the spec in the same PR.

## Stack (locked for v1)
- Python 3.12, FastAPI, **Pydantic AI**, SQLite, Pillow + imagehash
- Vision: `google:gemini-*-flash` default, `anthropic:claude-haiku-*` escalation, selected by `MODEL` env var
- Client: single-file PWA (`static/index.html`), vanilla JS camera API, tap-to-scan in v1
- Dev tunnel for HTTPS on the phone: `cloudflared tunnel --url http://localhost:8000`

## Owner constraints
- Current laptop is weak → no local model inference; all inference is cloud.
- Raspberry Pi (basic kit, no camera yet) is an optional later client, not required.
- Nothing in this project should depend on the owner's POS/Odoo work.
- Budget: ≤ 5 USD/month total.

## Roadmap (one Spec Kit feature each)
- **001 scan-frame** — tap to scan, typed objects, dedupe gate, budget guard, cost log. *(done 2026-10-21)*
- **002 smart-routing** — Gemini first, escalate to Haiku on weak results or unanswered questions; calibrated estimates from the ledger; per-model stats + panel in the page; provider spend limits documented. *(done 2026-10-21)*
- **003 live-overlays** — live loop with changed-and-still gate, data drawn on the frame, template tracking between results, flip, zoom. *(done 2026-10-21)*
- 003b (deferred) in-browser detector for 10-15 fps boxes, if live tracking still feels laggy

Order from here (decided 2026-10-21): 006 evals → 004 entity memory → 005 RAG → 007 Docker/CI/observability → 008 Pi.
- 004 entity memory — vector store of seen entities, "same mug as yesterday"
- 005 RAG tool — `lookup(label)` over a small knowledge base
- 006 evals — 50 labeled photos, scored on every prompt/model change
- 007 Docker Compose, CI, observability (traces, tokens, latency, cost)
- 008 Raspberry Pi client (optional)

## How to work in this repo
0. Solo repo: commit straight to `main`, no feature branches or PRs (decided 2026-10-21). Tests green before every commit. Each feature still gets `specs/NNN-name/` and a closing commit that marks the spec Done.
1. If Spec Kit commands are missing: `uv tool install specify-cli --from git+https://github.com/github/spec-kit.git` then `specify init --here --integration claude --script ps` (keep existing files). Already done once on 2026-10-21; commands live in `.claude/skills/speckit-*`.
2. `/speckit-constitution` — use `.specify/memory/constitution.md` as the input; keep its principles.
3. `/speckit-specify` for feature 001 — use `specs/001-scan-frame/spec.md` as the draft.
4. `/speckit-plan` → `/speckit-tasks` → `/speckit-implement`. The owner reviews every generated artifact before the next step; explain decisions briefly as you go — this project is for learning, not just output.
5. Never hardcode API keys; use `.env` (gitignored) with `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, `MODEL`, `BUDGET_USD=5`.
6. Keep the owner's coding preference: guide and explain, don't just dump code when a concept is new.
7. Evals discipline (feature 006): any change to `INSTRUCTIONS`, `LIVE_HINT`, `SCAN_MODEL_SETTINGS`, the model strings, `ESCALATE_BELOW` or `app/schema.py` is committed with the output of `uv run python -m app.evals run` in the message, compared to the previous report in `eval/runs/`.

## Open decisions
- Pydantic AI vs LangGraph: start with Pydantic AI; reconsider at feature 003 if workflows branch.
- bbox from the vision model vs YOLO: model bbox in v1 (optional field), YOLO considered in 003.
