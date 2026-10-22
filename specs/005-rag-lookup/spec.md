# Feature 005: RAG lookup — the agent consults a knowledge base when a question needs facts

**Status**: Done (2026-10-22; phone check: grounded answer with source, 'no note found' for an unrelated question; eval source accuracy 3/3, recall 0.90) | **Created**: 2026-10-22

## Why
Today a question is answered from the picture alone. "Is this plant healthy?" works; "how
often should I water it?" or "when does this yogurt expire once opened?" does not, because the
answer is not in the pixels. This feature gives the agent a small, owner-written knowledge
base and a **tool** it can call to look things up. Two concepts the project has not covered
yet: tool use (the model decides to call a function) and retrieval-augmented generation
(the function returns passages found by embedding similarity). Memory (004) already supplies
the embedding plumbing.

## User scenarios

### Primary
The owner keeps a folder of Markdown notes: plant care, appliance manuals, product notes,
house rules ("the blue mug is Dana's"). They point the camera at the cactus and ask "how often
do I water this?". The agent recognises the plant, calls `lookup("cactus watering")`, gets the
note's passage, and answers "about every three weeks in winter, per your note", with the
source shown on the card.

### Secondary
A question with no relevant note: the agent answers from the image and says it found no note,
rather than inventing one.

### Tertiary
The owner adds or edits a note; one command re-indexes; the next question uses it.

### Edge cases
- Lookup finds passages but none relevant (low similarity) → treated as "no note"; the answer
  says so. No hallucinated citations.
- Knowledge base empty or indexing failed → the tool reports "no knowledge base" and the scan
  proceeds as in 004.
- Tool call loops: at most 2 lookups per scan (usage limits), each capped to 3 passages.
- Live mode: tools are not offered (brief mode); only tap scans with a question can look up.
- Budget: index embeddings and per-scan extra tokens go through the ledger (tier `embedding`
  for indexing; the extra model round-trip is part of the scan's own call).

## Functional requirements
- FR-1 **Knowledge base.** `KB_DIR` (default `./kb`, gitignored except `kb/README.md`): Markdown
  files. Each file is split into passages (heading-delimited, ≤ ~800 chars), embedded with the
  004 embedding model and stored in SQLite (`passages(id, file, heading, text, embedding, mtime)`).
- FR-2 **Indexing.** `uv run python -m app.kb index` (re)indexes changed files by mtime/hash;
  `app.kb search "query"` prints the top passages for debugging. Indexing cost is on the ledger.
- FR-3 **Tool.** The agent gets a `lookup(query: str) -> list[Passage]` tool (Pydantic AI tool)
  returning up to 3 passages above a similarity floor, each with `file`, `heading`, `text`,
  `score`. Offered only on tap scans with a question (`brief=False`).
- FR-4 **Grounded answers.** Instructions: when the answer relies on a passage, say so and cite
  it; when lookup returns nothing relevant, say no note was found. `ScanResult` gains
  `sources: list[Source]` (`file`, `heading`) and the page shows them under the answer.
- FR-5 **Usage limits.** At most 2 tool calls per scan; the scan's `UsageLimits` grows to
  allow the extra request (3 requests total) with the same output cap.
- FR-6 **Memory + KB together.** The memory context (004 FR-6) stays; the lookup tool may be
  called with an entity's identity text to find notes about that specific object ("Dana's mug").
- FR-7 **Evals.** The set gains questions whose answers live only in the KB (`kb/` sample notes
  committed for the eval), scored by `expect_answer_contains` plus a new `source_expected` check.

## Changes during implementation (constitution VI)
- Private-data rule: the lookup tool, memory context, entity linking and cached question answers
  are only given to callers that pass the local-or-token check; others get a plain scan from a
  separate cache. Tool-enabled scans reserve 2x the estimate.
- Sample notes for the evals live in `eval/kb/` (committed); the owner's notes in `kb/` stay
  gitignored. The eval runner indexes `eval/kb` by default (`--kb ''` to disable).

## Non-goals (this feature)
Web search; large documents (PDF manuals: a later feature could add a PDF-to-Markdown step);
re-ranking models; per-user knowledge bases; editing notes from the phone.

## Acceptance
- Unit tests: chunking, index incrementality, search ranking with the hashing embedder, tool
  behaviour (hit / no hit / empty KB), usage limit, response shape; API test with the fake model.
- Manual: two notes (a plant, a mug), two questions on the phone: one grounded with a source,
  one answered "no note found".
- Evals: the KB questions answered with the right source; recall unchanged.
