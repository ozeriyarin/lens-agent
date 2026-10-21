# Plan: 002 smart-routing

## Architecture change
```
main.py /scan  ──▶ gate ──▶ cache ──▶ router.run(frame, question)
                                        ├─ budget.reserve(primary)  → agent.scan(primary)  → settle
                                        ├─ policy.is_weak(result, question)?  no → done
                                        └─ budget.reserve(escalation) → agent.scan(escalation) → settle
                                             (BudgetExceeded / AgentFailed → keep primary result)
```
The reserve/settle/fail choreography moves out of `main.py` into a new `app/router.py`
so `/scan` stays a thin HTTP adapter and the routing is unit-testable without FastAPI.

## Decisions
- **Whole-frame escalation, one tier.** Simplest policy that satisfies constitution III.
  Per-object or multi-tier routing would need confidence calibration we don't have yet
  (that is what evals, feature 006, will give us).
- **Weakness = structural, not semantic.** "No objects / low max confidence / unanswered
  question" can be computed from the typed result alone. No second model call to judge the
  first one; that would cost what it saves.
- **Calibration lives in `Budget`.** It already owns the ledger; `estimate_for(model)` reads
  the last settled rows. The fixed table stays as the cold-start fallback and as the floor
  for unknown models.
- **Stats panel is plain DOM, no chart library.** Numbers and a short table are enough; a
  chart would be feature 007 (observability) material.
- **Settings gain `escalation_model` and `escalate_below`.** Empty escalation model disables
  routing, which keeps 001 behaviour reachable and the demo mode (`MODEL=test`) unchanged.

## Risks
- Haiku pricing is ~3-4x Gemini Flash paid tier; escalation on every frame would eat the
  budget. Mitigation: threshold default 0.6, escalation count surfaced in `/stats`.
- Confidence scales differ between models; the threshold only ever judges the *primary*
  model's output, so the comparison is consistent.
- Two reservations per scan double the ledger write rate; still trivial for SQLite.
