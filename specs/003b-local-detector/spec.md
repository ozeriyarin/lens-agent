# Feature 003b: Local detector — boxes from the phone, meaning from the model

**Status**: Built (2026-10-22; pane check passed: bottle detected at Gemini's position, Gemini object attached, 7-9 fps; phone check pending) | **Created**: 2026-10-22

## Why
Live mode (003) draws boxes from the last cloud result and slides them with a template
tracker until the next one. That is a few seconds of staleness per view and a tracker that
drifts on faces. A small detector running *in the browser* on the phone's GPU gives fresh
boxes 5-15 times a second for free, with no frame leaving the device. The cloud model keeps
what only it can do: rich labels, identities, mood and age, answers, memory and notes. This
is the edge-to-cloud split the project set out to demonstrate.

## User scenarios

### Primary
Live is on. Boxes follow people, cups, bottles, laptops and phones smoothly as the camera
moves. When a Gemini result arrives, its data (label, "known · seen 3×", mood, identity)
attaches to the matching local box and moves with it. Objects the local detector knows but
Gemini has not described yet show a thin box with the detector's own class name.

### Secondary
A new object enters the frame (someone puts a cup on the desk). The detector sees a class
appear; the loop sends a frame without waiting for the view to "change enough". The cup gets
its Gemini card within a few seconds.

### Tertiary
An old phone, or a browser without WebGL: the detector fails to load; live mode works exactly
as in 003 (template tracker), and the status line says "local boxes: off".

### Edge cases
- Detector loads slowly (first visit, ~6 MB): live mode starts with the tracker, then switches
  to local boxes when ready, with a status line note.
- Detector and Gemini disagree on a box (different object boundaries): the local box wins for
  position/size; the Gemini label wins for meaning.
- Many detections (a cluttered shelf): draw at most 12, highest score first.
- Battery: detection runs only while live is on and the tab is visible, at a capped rate.

## Functional requirements
- FR-1 **Detector.** TensorFlow.js COCO-SSD (`lite_mobilenet_v2`) loaded from a CDN on the
  first Live start, cached by the browser afterwards. Runs on the live video at ≤ 8 fps on a
  downscaled frame; returns class, score, box. Score floor 0.5.
- FR-2 **Local boxes drawn.** Detections are drawn every detector tick with the detector's
  class in a thin, dimmer style; a `local` toggle in the controls turns the detector off (then
  003 behaviour).
- FR-3 **Fusion.** When a scan result arrives, each Gemini object with a box is matched to the
  local detection with the highest IoU ≥ 0.3 whose class is compatible with the object's
  category (COCO→category map; `other` matches anything). Matched pairs: the Gemini data is
  attached to the local detection and follows it on every detector tick (position from the
  detector, text from Gemini). Unmatched Gemini objects fall back to the template tracker.
- FR-4 **Identity across ticks.** Detections are tracked tick to tick by IoU (greedy, ≥ 0.4)
  so attached Gemini data persists while the object stays in view; it is dropped after the
  object is unseen for 1.5 s.
- FR-5 **Detector-driven trigger.** When the set of detected classes changes and stays changed
  for 2 consecutive ticks (a class appears or disappears), the live loop treats the view as
  changed and still, and sends a frame (subject to one-in-flight and budget).
- FR-6 **Status.** The status line shows detector fps and the number of local boxes while live.
- FR-7 **Privacy and cost.** Detection is entirely in the browser; nothing new leaves the
  phone; no server change is needed beyond the 003 contract.

## Non-goals (this feature)
Custom-trained detectors (the pantry-vision project has that lane); face landmarks; running
the detector in tap mode; server-side detection.

## Acceptance
- Pane check: a real fridge photo drawn into the source canvas yields `bottle` detections with
  boxes; a scan result's "Heineken bottle" attaches to one of them and reports `local: bottle`.
- Phone: boxes follow a moving cup at ≥ 5 fps; person box tight; a cup placed on the desk
  triggers a scan within 3 s; with `local` off, 003 behaviour returns.
- No new server tests needed (no server change); the 003 spec gains a pointer here.
