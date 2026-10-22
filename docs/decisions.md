# lens-agent: the decisions, with the numbers

A reviewer's walk through what was built in two days (2026-10-21/22), why each piece is
the way it is, and what the measurements said. Everything below is in the repo: specs
under `specs/`, eval reports under `eval/runs/`, the commit history for the rest.

## What it is

Point a phone camera at a scene; a server-side agent built with Pydantic AI returns a typed
list of objects (label, category, position, confidence, box; mood and age *estimates* for
people), answers questions, remembers objects it has seen, looks things up in the owner's
notes, and draws all of it on the live video. Hard budget: 5 USD a month, enforced in code.

## Spec-driven, feature by feature

Each feature is a folder with `spec.md` (what, as numbered requirements), `plan.md` (how,
with trade-offs) and `tasks.md`, written *before* code and updated in the same commit when
the code taught us something. Eight features (001-007 and 003b) shipped this way. The spec
files carry the phone-test findings that changed the design, so the record is honest, not
retrofitted.

## 1. Typed outputs are the contract (001)

The model never returns free text. Pydantic AI is handed `ScanResult` as `output_type`; it
sends the JSON schema, validates the reply, and retries once with the validation error. Our
code only ever sees a valid object or an exception. Closed vocabularies for category,
position, mood and age range keep the UI simple, make evals comparable, and (as a security
review pointed out) stop model output from carrying markup into the page.

**Box format matters more than prompt wording.** Asking Gemini for `x, y, w, h` gave boxes on
the wrong part of the face. Asking for its trained convention, `[ymin, xmin, ymax, xmax]` on
0..1000, and converting server-side, gave 13/13 tight boxes on a fridge-door photo. The
model-facing field is excluded from the API; clients still get normalised `bbox`.

## 2. The budget is a ledger with reservations, not a counter (001, 002)

Before any paid call the server *reserves* an estimate in SQLite inside a `BEGIN IMMEDIATE`
transaction (sum + insert atomically), then *settles* with real tokens, or marks the row
*failed* keeping max(estimate, observed usage) because the provider bills failed attempts
too. A test races eight threads at a 1 USD cap and exactly one gets through.

Calibration uses the **maximum** of the last 20 settled calls times 1.25, not the mean: a low
estimate lets concurrent calls overshoot the cap, and overshooting is the one direction the
constitution forbids. The provider-side spend limit is documented as the second line of
defence. Two days of development, evals and phone testing cost **0.09 USD**.

## 3. Pay only for new information (001, 002, 003)

- Frames are downscaled to 1024 px and perceptually hashed; a near-duplicate frame with the
  same question is served from cache for free. The hash is now 30 lines of numpy, verified
  bit-identical to the `imagehash` library it replaced (which cost 150 MB of scipy in the image).
- Routing: Gemini Flash first; Claude Haiku only when the result is *weak* (no objects, best
  confidence under 0.6, or a question nobody answered). Weakness is read off the typed result,
  not judged by a second model call. A near-black frame escalates; a clear desk does not.
- Live mode sends a frame only when the view **changed since the last send and has stopped
  moving**. The first version used an absolute stillness threshold; the phone logs showed 7
  frames in 6 minutes of panning because a hand never holds that still. Stillness became
  relative, with a "send on the 3rd tick anyway" rule, and a 6 s refresh while a person is in
  frame because expressions change without pixels moving. Each rule is a numbered FR with the
  log evidence next to it.

## 4. Latency: turn thinking off (003)

The phone logs showed 600-2500 output tokens and 4-13 s per live frame for a JSON of a few
objects. That was Gemini's hidden reasoning, billed as output. `thinking: False` plus a brief
live prompt: **~440 tokens and 2-3 s**. Boxes that had looked "misplaced" were mostly stale.

## 5. Edge and cloud split the work (003, 003b)

Between cloud results the phone keeps the picture honest. First a template tracker
(brightness-normalised matching of each box's crop, drift-capped), then a real detector:
COCO-SSD via TensorFlow.js, ~6 MB, on-device. Its boxes drive positions; Gemini's data
attaches to the matching box by IoU and category and follows it; a class appearing in the
frame triggers a scan. Nothing leaves the phone and it costs nothing.

The first phone run with the detector broke the video: a WebGL context next to the live
`<video>` on iOS stalled rendering (camera squeezed into a corner, one frame sent, then
silence in the logs). Moving the detector to the WebAssembly backend fixed it; a watchdog
throttles or disables it on slow devices.

## 6. Memory holds descriptions, never pixels (004)

Every object gets an `identity` from the model, the details that distinguish *this* one
("white ceramic mug, blue rim, chipped handle"). The text is embedded (Gemini embeddings,
through the same ledger) and matched by cosine against remembered entities of the same
category: link above 0.86, a near-tie counts as new, sightings rate-limited to one a minute.
People are never remembered. On the eval photos the six objects that appear in several
pictures linked with **identity recall 1.0 and precision 1.0**.

## 7. Tool use and retrieval (005)

The owner's Markdown notes are chunked per heading, embedded and searched by cosine with a
relevance floor. On a tap scan with a question the agent gets a `lookup` tool it may call
twice; answers cite the passages they used (`sources` is validated: only passages actually
returned can be cited). The eval's three note questions were answered with the right source,
3/3; an unrelated question gets "no note found" rather than an invention.

## 8. Evals decide, not impressions (006)

A labelled set (JSONL, photos kept outside the repo), a scorer (alias matching, IoU on
hand-checked boxes, answer and source checks, identity recall/precision) and a runner that
drives the real router. Rule in `CLAUDE.md`: any prompt, model or schema change is committed
with the eval numbers. Baseline recall 0.853 → 0.864 after adding identities → **0.900**
after the lookup instructions, with 0 hallucinations from the forbid lists.

## 9. Privacy and the security reviews (001-005)

Raw frames never touch disk: multipart uploads were switched to a raw body after a review
found the framework spools parts over 1 MB to temp files. Memory, notes and cached answers to
questions go only to callers that pass a local-or-token check; other callers get a plain scan
from a separate cache. Memory text re-enters the prompt only inside a delimited block the
instructions declare as data. Every review finding and its fix is in the commit messages.

## 10. Shipping (007)

`docker compose up`, a `/data` volume for ledger, memory and notes, CI (ruff, 145 tests with
fake models, image build) green on the first push, one JSON event line per scan with timings,
tokens and cost joined to the ledger by `X-Request-Id`, optional Logfire tracing. The image
went 1.27 GB → 758 MB (a `chown -R` had duplicated the venv layer) → **447 MB** (numpy hash,
no uvicorn extras, two-stage build).

## What I would do next

Grow the eval set past seven photos; swap text-identity for image embeddings if look-alike
objects start merging; a Raspberry Pi client to make the edge-to-cloud story literal.
