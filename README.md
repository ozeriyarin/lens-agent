# lens-agent

[![ci](https://github.com/ozeriyarin/lens-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/ozeriyarin/lens-agent/actions/workflows/ci.yml)

Point your phone camera at a scene, tap **Scan**, get a typed list of what's there:
label, category, position, confidence, optional bounding box, and for people an
*estimated* mood and age range. Ask a question about the scene and the answer lands
on the right card.

A portfolio AI-engineering project built spec-first with
[GitHub Spec Kit](https://github.com/github/spec-kit): typed outputs, cost control, routing,
live mode with an on-device detector, entity memory, RAG lookup, evals, Docker and CI.
**Start with [docs/decisions.md](docs/decisions.md)**: the decisions, with the numbers
(latest eval: 37 photos, recall 0.785, 0 hallucinations, answers 5/5, cited sources 3/3).
[CLAUDE.md](CLAUDE.md) has the roadmap; each feature lives under [specs/](specs/).

## How it works

```
phone (static/index.html)
   │  POST /scan  (jpeg + optional question)
   ▼
app/main.py  ──▶ gate.prepare   downscale to ≤1024px, perceptual hash   (FR-3)
             ──▶ cache.lookup   same scene + same question → free reply (FR-4)
             ──▶ budget.reserve atomic check against the 5 USD/month cap (FR-5)
             ──▶ agent.scan     Pydantic AI → validated ScanResult        (FR-2)
             ──▶ budget.settle  real tokens, cost, latency → SQLite        (FR-6)
```

Non-negotiables (from the [constitution](.specify/memory/constitution.md)):
typed outputs everywhere, a hard **5 USD/month** cap enforced in code, pay only for
new information, raw frames never touch disk, tests for every gate.

## Run it with Docker (one command)

```bash
cp .env.example .env          # fill in GOOGLE_API_KEY; optional ANTHROPIC_API_KEY, SCAN_TOKEN
docker compose up --build     # http://localhost:8000
```

Without a `.env` the container runs in demo mode (`MODEL=test`). The ledger, memory and notes
live in the `lens-data` volume (`/data`), so restarts and rebuilds keep them; drop notes into
the volume's `kb/` or mount your own folder there.

## Run it

Requirements: Python 3.12 (managed by [uv](https://docs.astral.sh/uv/)), a phone with
a browser, and for real scans a Gemini or Anthropic API key.

```bash
uv sync                      # creates .venv with Python 3.12 and all deps
uv run pytest                # ~80 tests, no network needed
```

### 1. Demo mode, no key, no spend

```bash
MODEL=test uv run uvicorn app.main:app --port 8000
```

Open <http://localhost:8000>. The server answers with a canned desk scene from
Pydantic AI's offline `TestModel`, so you can exercise the whole UI and the ledger.
(PowerShell: `$env:MODEL='test'; uv run uvicorn app.main:app --port 8000`.)

### 2. Real model

```bash
cp .env.example .env         # then fill in GOOGLE_API_KEY (and/or ANTHROPIC_API_KEY)
uv run uvicorn app.main:app --port 8000
```

| variable | default | meaning |
|---|---|---|
| `MODEL` | `google:gemini-2.5-flash` | Pydantic AI model string. `anthropic:claude-haiku-4-5` for the escalation tier, `test` for demo mode. |
| `BUDGET_USD` | `5` | Monthly cap. A scan that would exceed it gets HTTP 402 and no model call. |
| `DB_PATH` | `lens.db` | SQLite spend ledger, memory and note index (gitignored). `/data/lens.db` in Docker and on Fly. |
| `KB_DIR` | `kb` | Folder of Markdown notes for the lookup tool. `/data/kb` in Docker and on Fly. |
| `SCAN_TOKEN` | empty | Shared secret for `/scan` and `/stats`. **Set it before tunnelling.** |
| `CACHE_SIZE` | `32` | How many recent scenes the dedupe cache remembers. |

| `ESCALATION_MODEL` | `anthropic:claude-haiku-4-5` | Second tier (feature 002). Empty disables escalation. Needs `ANTHROPIC_API_KEY`; without it escalation is silently off. |
| `ESCALATE_BELOW` | `0.6` | A primary result whose best object confidence is below this escalates. |

### Second line of defence: provider spend limits

The in-code cap stops *this* program from spending more than `BUDGET_USD`. It cannot stop
a leaked key, a second deployment pointed at the same account, or a bug in the estimate.
So set a hard limit at each provider too; takes two minutes and makes the worst case a
known number:

- **Google AI Studio / Gemini API**: [Google Cloud Console → Billing → Budgets & alerts](https://console.cloud.google.com/billing/budgets).
  Create a budget for the project, e.g. 5 USD/month with alerts at 50 / 90 / 100 %. Note that a
  Cloud *budget* only alerts; to actually cut off, keep the key on the free tier (no billing
  account attached) or add a budget action that disables billing.
- **Anthropic**: [Console → Settings → Limits](https://console.anthropic.com/settings/limits).
  Set the monthly spend limit (e.g. 5 USD). This one is a real cut-off.

Rule of thumb from the constitution: the code refuses *before* a call, the provider limit
refuses *regardless* of the code. You want both.

### 3. On the phone

Browsers only expose the camera over HTTPS. Two ways to get it:

**Live server (Fly.io).** The repo ships `fly.toml`; [docs/deploy.md](docs/deploy.md) has the
eight commands to create the app, the 1 GB volume at `/data` and the secrets, and how to roll
back. Hosting cost: a `shared-cpu-1x` 512 MB machine that stops itself when idle, about
0.5 to 3.5 USD/month depending on use, plus 0.15 USD for the volume. That is **on top of** the
5 USD/month cap in the constitution, which covers model (API) spend only. Vercel does not fit
(no long-running process, no disk); Render would be about 7.25 USD/month for the same thing.

**Tunnel from the laptop.** For a one-off session, expose localhost instead:

```bash
cloudflared tunnel --url http://localhost:8000
# or, if you already use ngrok:
ngrok http 8000
```

Set `SCAN_TOKEN` in `.env` first, then open the tunnel URL on the phone **once** as
`https://<tunnel-host>/#token=<your token>`. The fragment never reaches the server or its logs; the page moves it into
local storage and strips it from the address bar. Allow camera access, point, tap Scan.

## Endpoints

- `POST /scan` body = the JPEG/PNG bytes (`Content-Type: image/jpeg`, ≤12 MB), optional
  `X-Question` header (URL-encoded, ≤500 chars), optional `X-Live: 1` (sent by the live loop)
  → `ScanResponse` `{result, cached, request_id, model, primary_model, escalated, cost_usd, latency_ms}`.
  Errors: 400 bad image, 401 token, 402 budget reached, 413 too large, 415 not an image body, 502 model failed.
- `GET /stats` cap, month-to-date, remaining, cache hit/miss counters, recent ledger rows.
- `GET /health` `{status, model, version}`, no token: the host's liveness probe.
- `GET /` the PWA. Interactive API docs at `/docs`.

## Routing (feature 002)

Every scan runs on `MODEL` first. If the result is *weak* (no objects, best confidence below
`ESCALATE_BELOW`, or a question nobody answered) and `ESCALATION_MODEL` is configured, the same
frame is sent once more to the escalation tier and that result is returned, with `escalated: true`
and both calls in `cost_usd`. Escalation never runs when the primary call failed, when the budget
cannot cover it, or in demo mode. Tap the cost in the page header for per-model stats.

## Live mode (feature 003)

Tap **Live** and the page captures a frame every 1.5 s (adjustable 1-5 s). Before sending, it
compares a 32x24 grayscale thumbnail with the last frame it *sent*; if the mean pixel change is
below `MOTION_THRESHOLD` (6/255) the frame is skipped for free. Frames that do go out carry
`X-Live: 1` so the ledger can tell live scans from taps, and at most one request is ever in
flight. Results are drawn on the video: a box per object with label and confidence, mood and
age estimates on people, objects without a box in a strip, the scene sentence and any answer
along the bottom. The header shows live spend and a per-minute projection; a 401 or 402 stops
live mode with the reason.

Between cloud results, **local boxes** (feature 003b) keep the picture honest: a small COCO-SSD
detector (TensorFlow.js, loaded from a CDN on the first Live start, ~6 MB, cached) runs on the
phone's GPU at up to 8 fps. Its boxes drive positions; each Gemini object attaches to the matching
local box and follows it; objects the detector has no class for use the template tracker; detections
Gemini has not described yet show a thin grey box with the detector's class. A class appearing or
disappearing triggers a scan. Nothing leaves the phone; `cloud boxes only` turns it off. Nothing is recorded or stored: each frame lives in memory for one
request.

## Memory (feature 004)

The agent remembers objects, not images. For every detected object the model writes an
`identity` (what makes this one different: colour, brand, marks); the text is embedded
(`EMBEDDING_MODEL`, default `google:gemini-embedding-001`, under the same budget) and matched
against remembered entities of the same category (cosine ≥ `MEMORY_THRESHOLD`, default 0.86,
with a near-tie treated as new). Cards and frame labels show "known · seen N×"; with a question,
a summary of recently seen entities is given to the model as delimited data so "have you seen
this before?" works. `GET /entities` lists memory, `DELETE /entities/{id}` or `/entities` forgets;
these routes answer only locally unless `SCAN_TOKEN` is set. People are never remembered.
`MEMORY=off` disables all of it.

## Notes and lookup (feature 005)

Put Markdown notes in `kb/` (gitignored except its README): plant care, who owns what, how to
use things. `uv run python -m app.kb index` embeds them into passages (one per heading), and
on a tap scan with a question the agent gets a `lookup` tool it can call once or twice when the
answer is not in the picture. Answers that rely on a passage cite it; the page shows
"from your notes: file › heading" under the answer. No relevant passage → the answer says no
note was found. Live mode never offers the tool, and neither memory nor notes are used for
callers that fail the local-or-token check. The evals use the sample notes in `eval/kb/`.

## Evals (feature 006)

Unit tests prove the code; evals measure the model. `eval/set.jsonl` lists photos (kept outside
the repo in `EVAL_PHOTOS_DIR`, default `C:/ai-projects/lens-agent-eval-photos`) with the objects
that must be found, aliases, optional hand-checked boxes, things the model has wrongly claimed
before, and optional questions with expected answers.

```bash
uv run python -m app.evals run --tag baseline      # real models, ~0.003 USD/photo, report in eval/runs/
uv run python -m app.evals run --model test        # offline, checks the scorer
uv run python -m app.evals bootstrap               # draft labels for new photos; then edit, drop "draft": true
uv run python -m app.evals compare eval/runs/A.json eval/runs/B.json
```

Metrics: recall of expected objects, forbid-hit rate (hallucinations), mean IoU on boxed
objects, answer accuracy, escalation rate, cost per photo, latency p50/p95, and the list of
what was missed per photo. Rule: any change to the instructions, model settings, model strings,
escalation threshold or schema is committed with the eval summary in the message.

## Observability (feature 007)

Every `/scan` writes one JSON line to the app log (`"event": "scan"`): request id, source,
status, cached, model(s), escalated, tokens in/out, cost, total latency and per-step timings
(gate, model, memory), entities linked, passages cited. The response carries the same id in
`X-Request-Id`, and the ledger row has it too, so a log line, a ledger row and a trace join.

```bash
docker compose logs -f lens | grep '"event": "scan"'
```

Traces: set `LOGFIRE_TOKEN` (free tier at logfire.pydantic.dev) and `uv add logfire`; Pydantic AI
and FastAPI are then instrumented and each model call shows as a span with its token usage.
Without the token nothing is sent anywhere.

CI (`.github/workflows/ci.yml`) runs ruff, the test suite with fake models (no keys, no spend)
and a Docker build on every push.

## Cost model

Before a scan the ledger reserves an estimate: the highest cost among the model's last 20
settled calls times 1.25, or the fixed `SCAN_ESTIMATE_TOKENS` table until there are 3 such calls
(`app/budget.py`). After the scan the reservation is replaced by the real token counts priced
from `PRICES_USD_PER_MTOK`. If the model fails after the
request went out, the larger of estimate and observed usage stays charged. The
reserve step runs inside a `BEGIN IMMEDIATE` transaction so concurrent scans
cannot both slip under the cap.

## Layout

```
app/schema.py   DetectedObject, ScanResult, ScanResponse   (the contract)
app/gate.py     prepare() + ScanCache                        (resize, pHash, LRU)
app/budget.py   Budget: reserve / settle / fail, SQLite     (the cap)
app/agent.py    build_agent(), scan()                        (Pydantic AI)
app/router.py   is_weak(), Router.run()                      (cheap first, escalate once)
app/memory.py   entities + sightings, link(), context()      (what the agent remembers)
app/kb.py       notes -> passages -> search(); the lookup tool   (what the agent can look up)
app/events.py   one JSON line per scan                           (what happened, how long, how much)
app/tracing.py  Logfire behind LOGFIRE_TOKEN                    (spans per model call)
app/embeddings.py embed() under the ledger                  
app/evals.py    eval set, scorer, runner, bootstrap, compare   (measure the model)
app/main.py     FastAPI composition, /scan, /stats           (the wiring)
app/config.py   Settings.from_env()
static/         index.html, the whole client
specs/          one folder per feature: spec → plan → tasks
eval/           set.jsonl (labels) and runs/ (reports); photos live outside the repo
tests/          one file per module, plus end-to-end API tests
```
