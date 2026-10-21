# Tasks: 001 scan-frame

- [x] T1 Schema: `DetectedObject`, `ScanResult` (app/schema.py) + tests
- [ ] T2 Budget guard: ledger table, `can_spend`, `record` (app/budget.py) + tests
- [ ] T3 Gate: resize, phash, LRU cache (app/gate.py) + tests
- [ ] T4 Agent: Pydantic AI agent with system prompt and `ScanResult` output (app/agent.py)
- [ ] T5 API: `POST /scan`, `GET /stats`, static hosting (app/main.py)
- [ ] T6 PWA: camera, scan button, question box, result cards (static/index.html)
- [ ] T7 Run locally with a real key, scan a desk, verify ledger rows
- [ ] T8 Tunnel to phone over HTTPS, verify camera works on mobile
- [ ] T9 Write README "how to run" from a clean machine
