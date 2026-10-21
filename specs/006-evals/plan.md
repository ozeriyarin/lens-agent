# Plan: 006 evals

## Architecture
```
eval/set.jsonl            labels (committed)         EVAL_PHOTOS_DIR/   photos (private, not committed)
        │                                                   │
        └──────────────▶ app/evals.py ◀────────────────────┘
                           ├─ load_set()         FR-1, drafts excluded
                           ├─ match()/iou()      FR-2
                           ├─ score_photo()      FR-3
                           ├─ summarize()        FR-4
                           ├─ run()              FR-5: gate.prepare -> Router.run per photo (no cache)
                           ├─ bootstrap()        FR-6
                           └─ compare()          FR-7
                        eval/runs/*.json         reports (committed: small, they are the evidence)
```

## Decisions
- **Reuse the production path.** The runner calls `gate.prepare` and `Router.run` exactly as
  `/scan` does (same resize, same budget ledger, same escalation), with its own SQLite ledger
  file so eval spend is visible and separate (`eval/ledger.db`, gitignored). Measuring a
  different code path than the one users hit would measure the wrong thing.
- **Labels in JSONL, photos outside the repo.** One line per photo diffs well in git; photos of
  the owner's home stay private (constitution IV spirit) and the set file names them by relative
  path so another machine only needs the folder.
- **Alias matching, not embeddings.** "coffee mug" vs "mug" vs "cup" is a synonym problem the
  owner can see and fix in the file; a semantic matcher would hide scoring bugs behind a model.
- **Extras are reported, not penalised.** The owner labels what matters per photo, not every
  object; punishing unlabelled-but-correct predictions would reward blindness. `forbid` is the
  explicit hallucination list for things the model has wrongly claimed before.
- **Box IoU only where the owner drew a box.** Boxes are expensive to label; recall is the
  headline, IoU is tracked on a subset.
- **Reports are JSON, committed.** They are small (KBs) and are the "before/after numbers" the
  commit messages cite; the compare command reads two of them.

## Risks
- Small set at first (7 photos) → noisy numbers. Mitigation: the bootstrap command makes adding
  photos a two-minute job; the report prints the set size next to every metric.
- Model nondeterminism → run-to-run jitter. Mitigation: temperature 0.2 already; compare shows
  deltas with the set size, and the owner runs twice when a change is small.
- Each real run costs ~N x 0.002 USD; at 50 photos that is 10 cents, 2 % of the monthly cap.
  The budget guard applies (own ledger, same cap).
