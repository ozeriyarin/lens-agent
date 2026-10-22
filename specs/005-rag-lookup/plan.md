# Plan: 005 rag-lookup

## Architecture
```
kb/*.md ──index──▶ app/kb.py  passages table (SQLite, same file as the ledger)
                      ▲ search(query) -> top-k by cosine (numpy, like memory)
agent.build_agent(..., tools=[lookup])   lookup() = kb.search wrapped as a Pydantic AI tool
   └─ tap scan with a question: tools offered; live/brief: no tools
ScanResult.sources ◀── the model lists which passages it used
page: "from your notes: <file> › <heading>" under the answer
```

## Decisions
- **Tool, not prompt stuffing.** Pasting every note into each prompt would be cheap at 5 notes
  and ruinous at 500, and it teaches nothing. A tool makes the model decide *when* it needs a
  fact, and the eval can count lookups.
- **Reuse memory's store pattern.** Same embedding wrapper, same SQLite file, same cosine
  search in numpy; `kb.py` is memory.py's shape without sightings. No vector database until
  the corpus is bigger than a home.
- **Heading-delimited chunks.** Owner-written Markdown has headings; a passage per heading
  (split further at ~800 chars) keeps citations meaningful ("plants.md › Cactus").
- **Similarity floor.** A floor (default 0.3 for the real model; configurable) turns "nearest
  passage" into "relevant passage", which is what stops the model citing a mug note for a
  watering question.
- **Tools only on tap scans.** Live mode is latency-bound and never has a question in practice;
  keeping it tool-free also keeps its cost estimate honest.

## Risks
- Pydantic AI tool semantics with structured output: the output tool and the lookup tool
  coexist; verify the retry/usage accounting with `FunctionModel` tests before relying on it.
- Gemini may call the tool when it does not need to (cost +1 round trip). The eval reports
  lookups per scan; tune the instruction wording if it is trigger-happy.
- Embedding model drift: if `EMBEDDING_MODEL` changes, the index must be rebuilt
  (store the model name with each passage; mismatch -> reindex).
