# Tasks: 006 evals

- [x] T1 Set format + matching + metrics: `app/evals.py` load_set, match, iou, score_photo, summarize; tests on hand-built cases
- [x] T2 Runner: `run` over the real Router with its own ledger, cache off, budget pre-check, report JSON; end-to-end test with the fake model and generated images
- [x] T3 Bootstrap + compare commands; tests
- [ ] T4 Starting set: bootstrap the 7 local photos into `eval/set.jsonl`; owner corrects labels; first real run committed as the baseline
- [ ] T5 Docs: README evals section, CLAUDE.md discipline rule, `.gitignore` for the eval ledger; close the spec
