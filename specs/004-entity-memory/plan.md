# Plan: 004 entity-memory

## Architecture change
```
router.run ─▶ ScanResult (objects now carry `identity`)
          ─▶ memory.link(result, request_id)          app/memory.py
                ├─ embed(["category: label. identity", ...])   one batched call, budget-gated
                ├─ for each object: nearest known entity of same category (cosine over SQLite rows)
                ├─ link or create; record sighting (rate-limited)
                └─ annotate result.objects[i].entity
          ─▶ /scan response; /entities endpoints; page badges + panel
agent._prompt ◀─ memory.context_for(question, result)   FR-6: top entities summarised into the prompt
```

## Decisions
- **Text identity + text embeddings, not image embeddings.** The vision model already looks at
  the object; asking it to write down what makes it distinctive ("white ceramic mug, blue rim,
  chip on handle") and embedding that text keeps the whole feature on the keys we have, costs
  nothing on the free tier, and keeps memory human-readable (the entities panel shows exactly
  what the agent remembers). If it proves too weak on look-alikes, Voyage multimodal-3 on
  object crops is the upgrade; the store schema does not change.
- **Cosine in Python over SQLite rows.** A home has hundreds of entities, not millions; a numpy
  dot product over all rows of a category is microseconds. No vector extension, no new service.
  Feature 005 (RAG) may introduce one if the knowledge base grows.
- **Same-category only, ambiguity = new.** Linking a mug to a laptop is never right, and a
  near-tie between two mugs is a coin flip; both rules make false links rare at the cost of
  some duplicates, which the owner can merge later by forgetting one.
- **Rate limit per entity (60 s).** Live mode would otherwise count sightings per frame.
- **Memory is after the scan, not in it.** The scan response is produced first; the memory step
  can fail without failing the scan. The only pre-scan use is the FR-6 context, which is read-only.

## Risks
- Identity text quality: a lazy "white mug" for every mug collapses all mugs into one entity.
  Mitigation: the instructions give examples and the evals measure identity presence/specificity.
- Embedding model availability under the Pydantic AI version in use; fall back to the provider
  SDK (`google-genai`) if the framework's embeddings API does not cover it.
- Privacy: identities may contain text read off objects (a name on a mug). Acceptable (the
  owner's own home), documented; people never get identities.
