# Plan: 003b local-detector

## Architecture (all in static/index.html)
```
live.start() ──▶ detector.load()  (tfjs + coco-ssd from CDN, once; async; fallback = off)
detector loop (≤8 fps, visible tab, live on):
   detect(video crop) ─▶ tracks: match to previous tick by IoU, keep ids, drop stale
                      ─▶ classesChanged? → live.trigger()                        FR-5
                      ─▶ drawOverlay(lastResponse, tracked = positions from tracks)
showResponse(resp) ──▶ fuse: Gemini objects ⇄ tracks by IoU + class map        FR-3
                      ─▶ unmatched Gemini objects → template tracker as in 003
```

## Decisions
- **COCO-SSD lite via TF.js**, not YOLO via ONNX Runtime Web. It is two script tags, ~6 MB,
  runs on WebGL everywhere a phone browser runs, and ships its own NMS. YOLOv8n in ONNX would
  be more accurate but needs WebGPU for speed and hand-written pre/post-processing; not worth
  it for a portfolio demo whose meaning comes from Gemini anyway.
- **Detector owns position, Gemini owns meaning.** The page never shows a Gemini box when a
  local box matched it: one source of truth for geometry, so boxes cannot disagree on screen.
- **Class compatibility map, not label matching.** COCO's 80 classes map onto our 10
  categories (person→person, cup/bottle/bowl→food|other, laptop/cell phone/tv→electronics, …).
  Label text is Gemini's business.
- **Keep the template tracker as the fallback** for objects COCO has no class for (a tahini
  jar is "bottle" at best, a humidity slider is nothing). Both paths feed the same overlay.
- **Trigger on class-set change, debounced by two ticks**, so a flickering detection does
  not cost a scan.

## Risks
- iOS Safari WebGL memory limits with TF.js: load the `lite` model, detect on a ≤ 320 px side
  canvas, dispose tensors; if load fails, fall back silently.
- CDN dependency for the page: the single-file promise bends (two external scripts). Pinned
  versions, loaded only when live starts, so tap mode stays dependency-free.
- CPU heat on long sessions: the fps cap and the visibility pause.
