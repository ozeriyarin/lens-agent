# Tasks: 005 rag-lookup

- [ ] T1 KB store: `app/kb.py` chunking, incremental index, search with floor; `python -m app.kb index|search`; tests with the hashing embedder
- [ ] T2 Tool: `lookup` registered on the agent for tap scans with a question; usage limits; `ScanResult.sources`; instructions for grounding; FunctionModel tests of call/no-call/empty
- [ ] T3 Wiring + page: kb in Services, sources under the answer card; API tests
- [ ] T4 Sample notes in `kb/` for evals + README; eval questions with `source_expected`; run and record
- [ ] T5 Phone check (grounded + no-note answers); close the spec
