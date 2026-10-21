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
uv run pytest                # 51 tests, no network needed
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

Also set a spend limit on the provider's billing page. The in-code cap is the first
line of defence, the provider limit is the second.

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

## Cost model

Before a scan the ledger reserves a rounded-up estimate (`app/budget.py`,
`SCAN_ESTIMATE_TOKENS`). After the scan the reservation is replaced by the real
token counts priced from `PRICES_USD_PER_MTOK`. If the model fails after the
request went out, the larger of estimate and observed usage stays charged. The
reserve step runs inside a `BEGIN IMMEDIATE` transaction so concurrent scans
cannot both slip under the cap.

## Layout

```
app/schema.py   DetectedObject, ScanResult, ScanResponse   (the contract)
app/gate.py     prepare() + ScanCache                        (resize, pHash, LRU)
app/budget.py   Budget: reserve / settle / fail, SQLite     (the cap)
app/agent.py    build_agent(), scan()                        (Pydantic AI)
app/main.py     FastAPI composition, /scan, /stats           (the wiring)
app/config.py   Settings.from_env()
static/         index.html, the whole client
specs/          one folder per feature: spec → plan → tasks
tests/          one file per module, plus end-to-end API tests
```
