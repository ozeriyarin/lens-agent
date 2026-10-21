# Feature 004: Entity memory — "the same mug as yesterday"

**Status**: Draft | **Created**: 2026-10-21

## Why
Today every scan is amnesic: the mug on the desk is a new mug every time. A vision *agent*
should recognise things it has met. This feature gives it a memory of entities: not photos
(constitution IV), but compact descriptions and embeddings of each object it has seen, so a
new detection can be linked to a known entity and the UI can say "seen 12 times, first on
Tuesday". It is also the project's first vector-search component, which feature 005 (RAG)
reuses.

## User scenarios

### Primary
The person scans their desk in the morning. The mug card shows a small "known · seen 12×"
badge. They scan a different mug from the cupboard: no badge, it is new; the next time it will
be known.

### Secondary
The person asks "have you seen this before?" while pointing at a plant. The answer uses the
memory: "Yes, 4 times, first on 2026-10-21; last time it was on the windowsill."

### Tertiary
The person opens the entities panel: a list of known entities with their description, category,
how often and when last seen, and a Forget button per entity (and Forget all).

### Edge cases
- Two different objects with the same label (two white mugs) → the identity description
  (shape, colour, marks, brand) must separate them; if it cannot, they merge and the owner can
  split by forgetting. Precision over recall: a doubtful match is reported as new.
- Live mode sends many frames of the same scene → memory updates at most once per entity per
  minute, so counts mean "distinct sightings", not frames.
- Embedding call fails or budget refused → the scan still succeeds, just without memory.
- Memory disabled (`MEMORY=off`) → behaviour as 003.

## Functional requirements
- FR-1 **Identity description.** `DetectedObject` gains `identity: str | None`: a short phrase
  with the details that distinguish *this* object from others of its kind (colour, material,
  brand, pattern, damage, text on it). The instructions ask for it on every object; evals
  measure that it is present.
- FR-2 **Embedding.** For each detected object with an identity, the text
  `"{category}: {label}. {identity}"` is embedded with a cloud text-embedding model
  (`EMBEDDING_MODEL`, default Gemini `gemini-embedding-001`, free tier) through the same
  budget guard, as one batched call per scan.
- FR-3 **Store.** SQLite table `entities(id, category, label, identity, embedding BLOB,
  first_seen, last_seen, sightings, last_position, last_scene)` plus `sightings(entity_id, ts,
  request_id, position, scene)`. No image bytes, ever.
- FR-4 **Linking.** A detection links to the nearest known entity of the same category when
  cosine similarity >= `MEMORY_THRESHOLD` (default 0.86) and no other known entity is within
  0.02 of that score (ambiguity = new). Otherwise a new entity is created. The response's
  object gains `entity: {id, known: bool, sightings, first_seen, last_seen}`.
- FR-5 **Rate.** Per entity, a new sighting is recorded only if the last one is older than 60 s.
- FR-6 **Questions use memory.** When a question is present, the agent receives a compact
  summary of the top linked entities ("mug: seen 12x since 2026-10-21, last on_table") as part
  of the prompt for that scan, so "have you seen this before?" can be answered.
- FR-7 **API.** `GET /entities` (list, newest last-seen first), `DELETE /entities/{id}`,
  `DELETE /entities` (all), token-guarded like the rest.
- FR-8 **UI.** "known · seen N×" badge on cards and on the frame label; an Entities panel next
  to the stats panel with Forget buttons.
- FR-9 **Evals.** The set gains two same-object pairs across photos (e.g. the Heineken bottle in
  home-01/02/06/07); a new metric reports whether repeated entities link (identity recall) and
  whether distinct ones stay apart.

## Non-goals (this feature)
Image embeddings (upgrade path: Voyage multimodal-3 of object crops, if text identity proves
too weak); cross-device sync; face recognition of people (never: people get mood/age estimates,
not identities).

## Acceptance
- Unit tests: store CRUD, cosine linking with the ambiguity rule, rate limit, memory-off path,
  response shape; API tests for /entities.
- Manual: scan the desk twice a minute apart, the mug is "known · seen 2×"; a different mug is
  new; the question "have you seen this before?" is answered from memory.
- Evals: identity recall on the Heineken bottle across the four fridge photos >= 0.75 with no
  false link between the mustard container and the tahini jar.
