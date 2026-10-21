# Feature 006: Evals — measure the agent, not just test the code

**Status**: Done (2026-10-21; baseline eval/runs/20261021T160725Z-baseline.json: recall 0.853, 0 forbid hits, answers 3/3) | **Created**: 2026-10-21

## Why
Three features in, every judgement about the agent ("boxes are tighter now", "the remote
gets missed", "escalation fires on dark frames") has been made by eye on a handful of photos.
Constitution V says agent quality is measured on a labelled set, run on every prompt or model
change. This feature builds that set and the scorer, so a change to the instructions, the
model string, the thinking setting or the routing threshold produces a number before it ships.

## User scenarios

### Primary
The owner edits the instructions. They run one command. A minute later they have a report:
label recall went from 0.78 to 0.84, hallucination rate unchanged, median box IoU up, cost per
photo +4 %, escalation rate down. They commit the change with those numbers in the message.

### Secondary
The owner adds ten new photos to the eval folder (taken with the phone's own camera app,
never through the agent). One command drafts labels for them from the model; the owner
corrects the drafts in a text editor; the set is now bigger.

### Tertiary
A run is made offline against the fake model to check the scorer itself (CI, later).

### Edge cases
- A photo file in the set is missing on this machine → skipped and reported, run continues.
- The model fails on a photo → scored as a miss for that photo, reported, run continues.
- Two runs on the same set and model should score identically apart from model nondeterminism;
  the report shows the model, settings hash and set size so runs are comparable.
- Budget: a run of N photos costs about N x one scan; the run refuses to start if the
  remaining monthly budget is below 1.5 x that estimate.

## Functional requirements
- FR-1 **Eval set format.** `eval/set.jsonl`, one JSON object per photo:
  `{"id", "file", "expect": [{"label", "aliases": [...], "category"?, "box": [ymin,xmin,ymax,xmax]?}],
  "forbid": [...], "question"?, "expect_answer_contains"?, "notes"?}`.
  `file` is relative to `EVAL_PHOTOS_DIR` (env, default `C:/ai-projects/lens-agent-eval-photos`),
  which is outside the repo: photos are private, labels are committed.
- FR-2 **Matching.** A predicted object matches an expected one when its label, lower-cased,
  equals or contains the expected label or any alias (word-boundary match), and the category
  matches if given. Each expected entry matches at most one prediction and vice versa.
- FR-3 **Metrics per photo.** recall = matched / expected; `forbid` hits (hallucinations the
  owner specifically listed); extras = predictions matching nothing (reported, not penalised:
  the set is not exhaustive); mean IoU over matched pairs that both have boxes; answer hit when
  `expect_answer_contains` is given; latency, cost, escalated, model used.
- FR-4 **Metrics per run.** mean recall, forbid-hit rate, mean IoU, answer accuracy, escalation
  rate, mean cost, p50/p95 latency; plus the list of photos with recall < 1 and what was missed.
- FR-5 **Runner.** `uv run python -m app.evals run [--set eval/set.jsonl] [--model ...] [--limit N]
  [--tag text]` runs the real Router (gate, budget, escalation included, cache disabled) and
  writes `eval/runs/<UTC timestamp>-<tag>.json` (full per-photo data) and prints the summary.
  `--model test` uses the offline fake model.
- FR-6 **Bootstrap.** `uv run python -m app.evals bootstrap <photo-dir>` scans each new photo
  once and appends draft entries (labels from the model, no aliases) to the set with
  `"draft": true`; the owner removes `draft` after correcting. Drafts are excluded from scoring.
- FR-7 **Compare.** `uv run python -m app.evals compare <run A> <run B>` prints the metric deltas.
- FR-8 **Discipline.** CLAUDE.md says: any change to `INSTRUCTIONS`, `LIVE_HINT`,
  `SCAN_MODEL_SETTINGS`, the model strings, `ESCALATE_BELOW` or the schema is committed with the
  eval summary in the message.

## Findings from the baseline
- Generic labels are the main miss: the tahini jar came back as "container" / "spice jar", the
  mustard as "container". Candidate instruction change for a later run: ask for brand or
  contents when readable. Not changed here, so the baseline stays the baseline.
- Eggs are listed individually ("egg" x6) in one photo and as one "eggs" in others; the set
  tolerates both via aliases, and the count question was still answered right.

## Non-goals (this feature)
A labelling UI; crowd labels; a benchmark against other products; CI wiring (007).
Mood/age scoring (no ground truth without a person consenting to labels).

## Acceptance
- Unit tests for matching, IoU, per-photo and per-run metrics on hand-built cases; runner and
  bootstrap tested end to end with the fake model and generated images.
- Real run on the starting set (the 7 local photos, labels corrected by the owner) produces a
  report; a second run with a deliberately worse instruction shows lower recall.
