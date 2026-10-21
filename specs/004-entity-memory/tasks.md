# Tasks: 004 entity-memory

- [x] T1 Schema + instructions: `identity` on DetectedObject (never for people), `entity` annotation model; instructions ask for distinguishing details; tests
- [x] T2 Embeddings: `app/embeddings.py` with a provider-backed `embed(texts) -> list[vector]` under the budget guard (price table entry), plus a deterministic fake for tests
- [x] T3 Store + linking: `app/memory.py` SQLite tables, `link()` with cosine, ambiguity and rate rules, `context_for()`; tests
- [x] T4 Wiring: router/main call memory after the scan; `/entities` endpoints; `MEMORY=off`; tests
- [x] T5 Page: known badge on cards and frame labels; Entities panel with Forget
- [ ] T6 Evals: identity presence metric; cross-photo link pairs in the set; run and record
- [ ] T7 Manual check on the phone; README; close the spec
