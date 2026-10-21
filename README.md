# lens-agent

Point your phone camera at a scene, tap **Scan**, get a typed list of what's there:
label, category, position, confidence, optional bounding box, and for people an
*estimated* mood and age range. Ask a question about the scene and the answer lands
on the right card.

A portfolio AI-engineering project built spec-first with
[GitHub Spec Kit](https://github.com/github/spec-kit): agents, structured outputs,
cost control, and later memory, RAG and evals. See [CLAUDE.md](CLAUDE.md) for the
roadmap and [specs/001-scan-frame](specs/001-scan-frame/) for the first feature.

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
| `DB_PATH` | `lens.db` | SQLite spend ledger (gitignored). |
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

Browsers only expose the camera over HTTPS, so expose localhost through a tunnel:

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
  `X-Question` header (URL-encoded, ≤500 chars)
  → `ScanResponse` `{result, cached, request_id, model, primary_model, escalated, cost_usd, latency_ms}`.
  Errors: 400 bad image, 401 token, 402 budget reached, 413 too large, 415 not an image body, 502 model failed.
- `GET /stats` cap, month-to-date, remaining, cache hit/miss counters, recent ledger rows.
- `GET /` the PWA. Interactive API docs at `/docs`.

## Routing (feature 002)

Every scan runs on `MODEL` first. If the result is *weak* (no objects, best confidence below
`ESCALATE_BELOW`, or a question nobody answered) and `ESCALATION_MODEL` is configured, the same
frame is sent once more to the escalation tier and that result is returned, with `escalated: true`
and both calls in `cost_usd`. Escalation never runs when the primary call failed, when the budget
cannot cover it, or in demo mode. Tap the cost in the page header for per-model stats.

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
app/main.py     FastAPI composition, /scan, /stats           (the wiring)
app/config.py   Settings.from_env()
static/         index.html, the whole client
specs/          one folder per feature: spec → plan → tasks
tests/          one file per module, plus end-to-end API tests
```
