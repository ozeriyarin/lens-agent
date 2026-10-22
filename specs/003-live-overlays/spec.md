# Feature 003: Live mode with the data drawn on the frame

**Status**: Done (2026-10-21; four phone feedback rounds folded in as FR-9..FR-15; in-browser detector deferred to a later feature) | **Created**: 2026-10-21

## Why
001 and 002 are tap-to-scan: one frame, one result, cards below the picture. The thing
people expect from a "vision agent" is to point the camera and *see* what it sees, on the
picture, as the scene changes. This feature adds a live loop and draws the detection data
on the frame itself. It is also the first real stress on constitution III: a frame every
second or two must not mean a paid call every second or two.

## User scenarios

### Primary
The person toggles **Live**. The camera keeps running; every ~1.5 s the page captures a
frame. If the scene has not visibly changed since the last sent frame, nothing is sent.
Otherwise the frame goes to `/scan`; the response is drawn over the live video: a box per
object with its label and confidence, and for people the mood and age estimates; the scene
sentence and any answer along the bottom edge. Boxes stay until the next result replaces
them and dim when they are older than a few seconds.

### Secondary
The person types a question while live. It is sent with every frame that is sent, so the
answer updates as the scene changes (e.g. "is the door open?").

### Tertiary
The person watches the cost: the header shows this session's live spend and a projection
per minute at the current send rate. Toggling Live off clears the overlay.

### Edge cases
- Scene changed slightly (lighting flicker, hand tremor) → client motion gate says "same",
  nothing is sent. If it does get sent, the server pHash cache answers for free.
- Budget reached (402) or token rejected (401) while live → live mode stops itself and says why.
- Network slow: a frame is never sent while the previous request is in flight (no pile-up).
- Model returns no bbox for an object → that object is listed in a corner strip on the frame,
  not boxed.
- Tab hidden / camera lost → live mode pauses, resumes on return.

## Functional requirements
- FR-1 **Live toggle.** A button switches between tap-to-scan (001) and live mode. Interval
  defaults to 1500 ms and is adjustable (1–5 s).
- FR-2 **Client motion gate.** Before sending, the page compares a 32×24 grayscale thumbnail of
  the frame with the last *sent* one; mean absolute difference below a threshold = unchanged,
  skip. The threshold is a constant in the page, tuned on the phone.
- FR-3 **One in flight.** At most one `/scan` request is outstanding; the loop skips ticks while
  waiting.
- FR-4 **Overlay rendering.** A `<canvas>` over the video draws, per object with a bbox: the
  rectangle, the label, the confidence as a percentage, and for people `mood` and `age_range`
  with their confidences. Objects without a bbox are listed in a strip. The scene sentence and
  the answer (if any) are drawn along the bottom. Text has a contrasting backing so it is
  readable on any scene.
- FR-5 **Staleness.** Overlay elements older than 4 s are drawn at reduced opacity; after the
  next result they are replaced entirely.
- FR-6 **Cost awareness.** The header shows live-session spend and a per-minute projection
  (cost of sent frames in the last 60 s). A 402 or 401 stops live mode with a visible message.
- FR-7 **Server stays as is.** No new endpoint. `/scan` is called with an `X-Live: 1` header so
  the ledger can tell live frames from taps (stored in a new `source` column), for later
  evals and stats.
- FR-8 **Overlay coordinates match the pixels.** Boxes are positioned on the same canvas size
  as the frame that was sent, scaled to the displayed video; aspect ratio is never cropped
  while live.
- FR-9 **Boxes follow their objects between model results** (added after the first phone test).
  On each result the page keeps a small grayscale crop per box from the frame that was sent;
  ~8 times a second it matches that crop near the box's last position in the live video and
  moves the box. A box that cannot be matched (object gone or changed) is dropped until the
  next result. No model call, no cost.
- FR-10 **Native box format.** The model is asked for `box_2d = [ymin, xmin, ymax, xmax]` on
  0..1000 (what Gemini was trained on); the server converts it to the API's normalized
  `bbox`. Boxes must be tight and cover the whole object.
- FR-11 **Send when changed *and* still.** A live frame goes out only when the view differs from
  the last sent frame by more than the chosen sensitivity (low/medium/high) *and* has stopped
  moving since the previous tick. Panning or shake never triggers a scan.
- FR-12 **Front camera.** A Flip control switches rear/front, mid-live or between scans; the
  selfie preview is mirrored with the overlay text drawn readable.
- FR-14 **Zoom.** Pinch on the preview or a slider zooms 1-4x (up to 8x when the camera track
  exposes hardware zoom). Digital zoom crops the centre of the frame for upload, motion gate and
  tracker alike, so boxes stay aligned with the zoomed preview. Works mid-live.
- FR-15 **Small objects.** Live frames allow up to 10 objects and the instructions say that small
  handheld things (remote, phone, keys) count; the first phone test missed a remote.
- FR-16 **Changes inside a box count.** Besides the whole-frame rule, each tracked box is compared
  on a finer thumbnail against the last sent frame; a change inside it (a smile in a face box, a
  hand on the mug) triggers a send even when the rest of the frame is unchanged. (Phone feedback:
  switching from neutral to smiling did not refresh the mood estimate.)
- FR-17 **Faces refresh on a timer.** While the last result contains a person, a frame is sent
  every 6 s even if the view is unchanged: expressions change without pixels moving much and
  the person box usually spans the frame, so neither the global nor the in-box rule sees a smile.
- FR-18 **Tracker drift cap.** A tracked box that moves more than a quarter of the frame from where
  the model placed it is dropped (template matching wanders on blurry faces).
- FR-13 **Fast scans.** Model "thinking" is off for scans and live frames ask for brief output
  (<= 6 objects, short scene): measured on the phone 2.1-2.8 s per frame instead of 4-13 s.

## Non-goals (this feature)
Object tracking between frames, per-object escalation, a local detector (YOLO) — revisit if
model bboxes prove too loose on the phone; entity memory (004); recording or storing frames
(never, constitution IV).

## Acceptance
- Server tests: `X-Live` header lands in the ledger `source` column; `/stats` splits
  tap vs live counts.
- Manual on the phone: live for 60 s on a static desk sends ≤ 3 frames; waving an object in
  sends a frame within one interval and the box follows; a person in frame shows mood/age
  estimates on the box; cost per minute shown; toggling off freezes the overlay.
- Manual: with the cap artificially set to the current spend + one call, live mode stops
  itself with the budget message after one frame.
