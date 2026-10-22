"""Knowledge base tests (feature 005): chunking, incremental indexing, search with a floor."""

import pytest

from app.budget import Budget
from app.embeddings import Embeddings, hashing_embed
from app.kb import MAX_PASSAGE_CHARS, KnowledgeBase, chunk_markdown

EMB = "google:gemini-embedding-001"


# --- chunking -------------------------------------------------------------------------------


def test_chunk_markdown_one_passage_per_heading_with_heading_path():
    text = "# Plants\n\nintro line\n\n## Cactus\n\nWater every three weeks.\n\n## Fern\n\nMist daily.\n\n# Mugs\n\nThe blue mug is Dana's.\n"
    chunks = chunk_markdown(text)
    assert [h for h, _ in chunks] == ["Plants", "Plants › Cactus", "Plants › Fern", "Mugs"]
    assert chunks[1][1] == "Water every three weeks."


def test_chunk_markdown_splits_long_sections_at_paragraphs_and_handles_no_headings():
    paras = [f"paragraph {i} " + "x" * 300 for i in range(6)]
    chunks = chunk_markdown("## Long\n\n" + "\n\n".join(paras))
    assert len(chunks) >= 3 and all(len(t) <= MAX_PASSAGE_CHARS for _, t in chunks)
    assert all(h == "Long" for h, _ in chunks)
    assert chunk_markdown("just text, no heading") == [("(top)", "just text, no heading")]
    assert chunk_markdown("## Empty\n\n") == []


# --- store ----------------------------------------------------------------------------------


@pytest.fixture
def kb(tmp_path):
    notes = tmp_path / "kb"; notes.mkdir()
    (notes / "plants.md").write_text("# Plants\n\n## Cactus\n\nWater the cactus every three weeks in winter.\n\n## Fern\n\nMist the fern daily; it likes shade.\n", encoding="utf-8")
    (notes / "house.md").write_text("# House\n\n## Mugs\n\nThe white mug with the blue rim is Dana's favourite mug.\n", encoding="utf-8")
    (notes / "README.md").write_text("notes about notes (never indexed)", encoding="utf-8")
    budget = Budget(tmp_path / "l.db", cap_usd=5.0)
    return KnowledgeBase(tmp_path / "l.db", Embeddings(EMB, budget, embed_fn=hashing_embed), notes, floor=0.2)


async def test_index_embeds_notes_and_is_incremental(kb):
    stats = await kb.index()
    assert stats == {"files": 2, "indexed": 2, "unchanged": 0, "removed": 0, "passages": 3}
    assert kb.count() == 3 and set(kb.files()) == {"plants.md", "house.md"}
    again = await kb.index()
    assert again["indexed"] == 0 and again["unchanged"] == 2
    (kb.kb_dir / "plants.md").write_text("# Plants\n\n## Cactus\n\nWater every month.\n", encoding="utf-8")
    (kb.kb_dir / "house.md").unlink()
    third = await kb.index()
    assert third["indexed"] == 1 and third["removed"] == 1 and kb.count() == 1
    ledger = kb.embeddings.budget.entries()
    assert ledger and all(e.tier == "embedding" for e in ledger)  # indexing is paid through the ledger


async def test_search_ranks_relevant_passage_first_and_applies_floor(kb):
    await kb.index()
    hits = await kb.search("how often to water the cactus")
    assert hits and hits[0].file == "plants.md" and hits[0].heading == "Plants › Cactus"
    assert hits[0].score >= hits[-1].score >= 0.2
    assert await kb.search("how often to water the cactus", floor=0.99) == []
    assert await kb.search("   ") == []
    [mug] = await kb.search("blue rim mug Dana", top_k=1)
    assert mug.file == "house.md"


async def test_disabled_kb_indexes_nothing_and_finds_nothing(tmp_path):
    kb = KnowledgeBase(tmp_path / "l.db", None, tmp_path / "nope")
    assert kb.enabled is False
    assert (await kb.index())["passages"] == 0 and await kb.search("x") == []


async def test_embedding_failure_keeps_the_old_index(kb):
    await kb.index()

    async def boom(texts):
        raise RuntimeError("down")

    kb.embeddings._embed_fn = boom
    (kb.kb_dir / "plants.md").write_text("# Plants\n\nchanged\n", encoding="utf-8")
    stats = await kb.index()
    assert stats["indexed"] == 0 and kb.count() == 3  # old passages for plants.md kept
