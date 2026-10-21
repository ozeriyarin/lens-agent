# Feature 001: Scan a frame and return typed objects

**Status**: Implemented (T7/T8 manual checks pending) | **Created**: 2026-10-21 | **Updated**: 2026-10-21

## User scenarios

### Primary
A person opens the web page on their phone, points the camera at a scene, taps "Scan", and within a few seconds sees one card per detected object with its label, category, position, and (for people) estimated mood and age range.

### Secondary
The person types a question ("is this plant healthy?") before scanning. The answer appears on the relevant card.

### Edge cases
- Nothing recognizable in the frame → one card: "Nothing identifiable".
- Same frame scanned twice → second result returns instantly from cache, no paid call.
- Monthly budget reached → a clear message, no call made.
- Model returns malformed output → retried once, then an error card. The attempt still
  counts against the budget (the provider billed it): the ledger keeps the larger of the
  reserved estimate and the observed usage, marked `failed`.
- Two scans arrive at the same moment near the cap → at most one is allowed through
  (the budget check and the reservation are one atomic step).
- Someone finds the public tunnel URL → with `SCAN_TOKEN` set they get 401 and spend nothing.

## Functional requirements
- FR-1: `POST /scan` accepts a JPEG (multipart) and optional `question` text.
- FR-2: Response is a `ScanResult` with a list of `DetectedObject`, each with `label`, `category`, `position`, `confidence`, optional `bbox` (normalized 0..1), optional `mood`, optional `age_range`, optional `answer`.
- FR-3: Images are resized to a max side of 1024px before any model call.
- FR-4: A perceptual hash of the resized image is compared with the last N hashes; a near-duplicate returns the cached result.
- FR-5: Before a paid call, the budget guard confirms month-to-date spend + estimated call cost ≤ cap.
- FR-6: Every model call logs model, input/output tokens, estimated cost, latency, and request id.
- FR-7: The page works from a phone browser over HTTPS (camera API requirement).
- FR-8: `question` is at most 500 characters; uploads at most 12 MB and 40 megapixels (checked
  from the header before decoding). Larger inputs are refused before any paid work.
- FR-9: When `SCAN_TOKEN` is configured, `/scan` and `/stats` require it as a Bearer token.
  The page adopts `#token=` (URL fragment, never sent to the server) once into local storage. Unset = open, for localhost use.

## Non-goals (this feature)
Live video, bounding-box overlays, entity memory, RAG, evals. Those are features 002–005.

## Acceptance
- Unit tests pass for schema, budget guard, gate.
- Manual: phone scan of a desk with a laptop and a mug returns ≥2 objects with sensible labels.
- Cost log shows one row per scan, duplicate scans show zero new rows.
