# Lens Agent Constitution

## Core principles

### I. Typed outputs everywhere
Every model call returns a Pydantic model, never free text. The schema is the contract between the agent and the UI. Changing a schema is a spec change.

### II. Budget is a hard limit
Monthly spend cap is 5 USD, enforced in code (`app/budget.py`) before any paid call, and mirrored by a provider-side spend limit. Every paid call logs tokens, model, and cost to SQLite. A request that would exceed the cap is refused with a clear error, never silently degraded.

### III. Pay only for new information
No paid call on a frame that is near-identical to the previous one (perceptual hash gate). Images are downscaled before sending. Cheapest capable model first; escalate only on low confidence or explicit follow-up.

### IV. Privacy by default
Raw frames are never written to disk. Only downscaled bytes live in memory for the duration of a request. Stored memory holds entity descriptions and embeddings, not images. Age and mood are reported as estimates with confidence, never as facts.

### V. Test the tools, eval the agent
Every tool and gate has unit tests. Agent quality is measured by an eval set of labeled photos, run on every prompt or model change. No feature is "done" without its tests passing.

### VI. Spec first
Each feature starts as `specs/NNN-name/spec.md`, then `plan.md`, then `tasks.md`, then code. When code drifts from the spec, the spec is updated in the same PR.

## Tech stack (locked for v1)
- Python 3.12, FastAPI, Pydantic AI (agent framework), SQLite
- Vision models: Gemini Flash (free tier) default, Claude Haiku as the escalation tier
- Client: single-file PWA (vanilla JS, camera API), no build step
- Spec-Driven Development with GitHub Spec Kit inside Claude Code

## Governance
This constitution overrides any other guidance. Amendments are a PR that edits this file and explains why.

**Version**: 1.0.0 | **Ratified**: 2026-10-21
