# Tasks: 003 live-overlays

- [x] T1 Server: `source` column (tap|live) on the ledger, `X-Live` header on `/scan`, `/stats` split by source; tests
- [ ] T2 Overlay renderer: `<canvas>` over the video, `drawOverlay(result, w, h)` with boxes, label + confidence, person mood/age, unboxed strip, scene/answer line, backing rectangles, staleness fade; used by tap-to-scan too (replaces the DOM boxes)
- [ ] T3 Live loop: toggle + interval control, thumbnail motion gate, one-in-flight rule, pause on hidden tab, `X-Live` header
- [ ] T4 Cost awareness: live-session spend and per-minute projection in the header; stop on 401/402 with message
- [ ] T5 Phone test over the tunnel: static desk ≤ 3 frames/min, moving object tracked, person estimates on box, forced-402 stop; tune the motion threshold
- [ ] T6 Docs: README live-mode section; close the spec
