# Feature 002: Smart routing — cheap model first, escalate only when needed

**Status**: Draft | **Created**: 2026-10-21

## Why
Feature 001 sends every scan to one model. Constitution III says *cheapest capable model
first; escalate only on low confidence or explicit follow-up*. This feature adds that
second tier, makes the budget estimate honest, and makes the cost story visible in the UI.
Live mode (003) multiplies call volume, so routing has to exist before it.

## User scenarios

### Primary
The person scans a cluttered or dim scene. Gemini Flash returns a weak result (nothing, or
only low-confidence guesses). The server automatically re-runs the frame on Claude Haiku
and the person sees the better result, with a small "escalated" badge and the combined cost.

### Secondary
The person asks a question Gemini could not answer ("what brand is this?"). The server
escalates once; the answer comes from Haiku.

### Tertiary
The person taps the cost in the header and sees a panel: month-to-date vs cap, spend and
calls per model, how many scans were escalated, cache hit rate, and the last few scans.

### Edge cases
- Escalation would exceed the budget → the primary result is returned as-is, `escalated=false`.
- Escalation call fails → the primary result is returned; the failed attempt stays charged.
- Primary call fails → escalation is **not** attempted (a failure is not "low confidence");
  the request errors as in 001.
- No escalation model configured (or no key) → routing is a no-op; behaviour equals 001.
- The cache stores whatever was finally returned, so a repeated frame never re-escalates.

## Functional requirements
- FR-1 **Routing policy.** A scan runs on `MODEL` first. It escalates to `ESCALATION_MODEL`
  when the primary result is *weak*: zero objects, or the highest object confidence is below
  `ESCALATE_BELOW` (default 0.6), or a question was asked and neither the scene nor any object
  carries an answer.
- FR-2 **Each paid call is gated separately.** The escalation call takes its own reservation
  against the cap, with the escalation model's own estimate.
- FR-3 **Response reports what happened.** `ScanResponse` gains `escalated: bool` and
  `primary_model: str`; `model` is the model whose result is returned and `cost_usd` is the
  sum of both calls.
- FR-4 **Calibrated estimates.** The reservation estimate for a model is the mean cost of its
  last 20 settled calls times a 1.25 safety factor, falling back to the fixed token table when
  fewer than 3 settled calls exist. Failed rows are excluded from the mean.
- FR-5 **Per-model stats.** `GET /stats` adds `per_model: [{model, calls, failed, cost_usd,
  avg_latency_ms}]` and `escalations` (count this month).
- FR-6 **Stats panel.** Tapping the cost in the page header opens a panel showing the FR-5 data
  and the last 10 ledger rows; it refreshes after every scan.
- FR-7 **Second line of defence documented.** README explains setting a monthly spend limit on
  Google AI Studio and the Anthropic console, and why the in-code cap is not enough on its own.

## Non-goals (this feature)
Live video, overlays, memory, RAG, evals. A third tier. Per-object escalation (always the
whole frame).

## Acceptance
- Unit tests: policy decision table, calibration fallback and mean, router behaviour for
  each edge case above, stats shape.
- Manual: a deliberately dark photo escalates on the real models and the ledger shows two
  rows (`google:` then `anthropic:`); a clear desk photo does not escalate.
- Manual: the stats panel opens on the phone and matches `/stats`.
