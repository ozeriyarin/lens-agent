# Tasks: 002 smart-routing

- [x] T1 Calibration: `Budget.estimate_for(model)` rolling mean with fallback; `per_model()` stats; tests
- [x] T2 Router: `app/router.py` with `is_weak()` policy and `Router.run()` (reserve/settle/fail for both tiers, all edge cases); `ScanResponse.escalated/primary_model`; settings `ESCALATION_MODEL`, `ESCALATE_BELOW`; tests
- [x] T3 API: `/scan` delegates to the router; `/stats` adds `per_model` and `escalations`; tests
- [x] T4 PWA: stats panel behind the header cost, refreshed after each scan; escalated badge on results
- [ ] T5 Docs + manual check: README spend-limit section and new env vars; dark photo escalates on real models, clear photo does not; close the spec
