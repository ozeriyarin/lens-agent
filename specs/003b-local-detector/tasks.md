# Tasks: 003b local-detector

- [x] T1 Detector: lazy load of tfjs + coco-ssd, detect loop at ≤8 fps on a downscaled crop, tick-to-tick tracks by IoU with ids and staleness, `local` toggle, fps in the status line, silent fallback
- [x] T2 Fusion: COCO→category map; on each scan result attach Gemini objects to tracks by IoU ≥ 0.3; overlay draws tracks (Gemini text where attached, class name otherwise); unmatched objects keep the template tracker
- [x] T3 Trigger: class-set change held for 2 ticks sends a frame; status says why
- [x] T4 Pane check with a real photo drawn into the source canvas; spec 003 pointer; README live-mode paragraph
- [x] T5 Phone check; close the spec
