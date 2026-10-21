# Plan: 003 live-overlays

## Architecture change
Almost entirely client-side; the server only learns to tag ledger rows by source.

```
static/index.html
  liveLoop()  every INTERVAL ms:
     capture thumbnail ──▶ motionGate.changed(prev)?  no → skip (free)
                            yes → if inFlight skip, else POST /scan (X-Live: 1)
     response ──▶ overlay.draw(result, frameSize)   boxes + text on <canvas id="overlay">
              ──▶ cards (as in 001), header cost/min
app/main.py  /scan reads X-Live → budget.reserve(..., source="live"|"tap")
app/budget.py  new column `source`; per_model/ stats split by source
```

## Decisions
- **Motion gate in the browser, pHash gate on the server, both kept.** The browser gate saves
  the upload (~100 KB) and the round trip; the server gate is the one that is tested and that
  guarantees no paid call on an unchanged scene even if a client misbehaves.
- **Mean absolute difference on a tiny thumbnail**, not a perceptual hash in JS: it is ten
  lines, runs in well under a millisecond, and "did enough pixels change" is exactly the
  question. The threshold is empirical; start at 12/255 and tune on the phone.
- **Canvas overlay, not DOM boxes.** 001 used absolutely-positioned divs for one frozen frame.
  Live mode redraws every result and needs text with backing rectangles and opacity fades;
  a 2D canvas does that in one `draw()` and never leaks DOM nodes.
- **Overlay canvas is sized to the video's intrinsic size** and CSS-scaled with it, so the
  normalized bbox maths is `x * canvas.width` with no cropping (`object-fit: contain` while live).
- **No new endpoint, no websockets.** A request every 1.5 s at most, usually skipped, is well
  within plain HTTP; streaming would add state to the server for no gain at this volume.
- **`source` column instead of a separate table**: it is one more fact about a paid call, and
  evals (006) will want to filter on it.

## Risks
- Gemini bbox quality in live frames may be loose; the spec allows listing unboxed objects in
  a strip, and the YOLO option stays on the table for a later feature.
- Phone CPU: capturing + thumbnail every 1.5 s is light; drawing text with backing per object
  is light. Battery is the real cost; the interval is user-adjustable for that reason.
- Live mode multiplies calls when the scene keeps changing (walking around): the per-minute
  projection in the header exists so the person can see it, and the cap still holds.
