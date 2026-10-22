"""Lookup tool tests (feature 005): the tool is offered only on tap scans with a question,
a hit is cited, a miss is reported, an empty KB returns nothing, limits hold."""

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agent import AgentFailed, build_agent, lookup_toolset, scan
from app.budget import Budget
from app.embeddings import Embeddings, hashing_embed
from app.gate import prepare
from app.kb import KnowledgeBase
from app.router import Router
from tests.test_gate import make_scene

EMB = "google:gemini-embedding-001"


@pytest.fixture
async def kb(tmp_path):
    notes = tmp_path / "kb"; notes.mkdir()
    (notes / "plants.md").write_text("# Plants\n\n## Cactus\n\nWater the cactus every three weeks.\n", encoding="utf-8")
    budget = Budget(tmp_path / "l.db", cap_usd=5.0)
    kb = KnowledgeBase(tmp_path / "l.db", Embeddings(EMB, budget, embed_fn=hashing_embed), notes, floor=0.2)
    await kb.index()
    return kb


@pytest.fixture
def frame():
    return prepare(make_scene(5))


def scripted_model(record: dict):
    """A model that calls lookup once when it is offered, then answers citing what it got;
    without the tool it answers directly."""

    def fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        record["tools_seen"] = [t.name for t in info.function_tools]
        record["requests"] = record.get("requests", 0) + 1
        last = messages[-1]
        returns = [p for p in last.parts if isinstance(p, ToolReturnPart)]
        if "lookup" in record["tools_seen"] and not returns and not record.get("called"):
            record["called"] = True
            return ModelResponse(parts=[ToolCallPart(tool_name="lookup", args={"query": record.get("query", "cactus watering")})])
        out = info.output_tools[0]
        if returns:
            passages = returns[0].content
            record["passages"] = passages
            if passages:
                p = passages[0]
                args = {"scene": "a cactus", "objects": [], "answer": f"{p['text']} (from your notes)",
                        "sources": [{"file": p["file"], "heading": p["heading"]}]}
            else:
                args = {"scene": "a cactus", "objects": [], "answer": "No note found; it looks healthy.", "sources": []}
        else:
            args = {"scene": "a cactus", "objects": [], "answer": "looks fine", "sources": [{"file": "made-up.md", "heading": "x"}]}
        return ModelResponse(parts=[ToolCallPart(tool_name=out.name, args=args)])

    return FunctionModel(fake, model_name="fake")


async def test_tool_is_offered_only_with_toolset_and_hit_is_cited(kb, frame):
    rec: dict = {}
    agent = build_agent(scripted_model(rec))
    out = await scan(agent, frame, "how often do I water this?", toolset=lookup_toolset(kb))
    assert rec["tools_seen"] == ["lookup"] and rec["called"]
    assert out.result.sources == [{"file": "plants.md", "heading": "Plants › Cactus"}] or \
        [(s.file, s.heading) for s in out.result.sources] == [("plants.md", "Plants › Cactus")]
    assert "three weeks" in out.result.answer and rec["requests"] == 2


async def test_without_toolset_no_tool_and_invented_sources_are_dropped(kb, frame):
    rec: dict = {}
    out = await scan(build_agent(scripted_model(rec)), frame, "how often do I water this?")
    assert rec["tools_seen"] == [] and out.result.sources == [] and rec["requests"] == 1


async def test_irrelevant_question_returns_no_passages(kb, frame):
    rec: dict = {"query": "quarterly tax filing deadlines"}
    out = await scan(build_agent(scripted_model(rec)), frame, "when are taxes due?", toolset=lookup_toolset(kb))
    assert rec["passages"] == [] and out.result.sources == [] and "No note" in out.result.answer


async def test_empty_kb_returns_nothing(tmp_path, frame):
    budget = Budget(tmp_path / "e.db", cap_usd=5.0)
    empty = KnowledgeBase(tmp_path / "e.db", Embeddings(EMB, budget, embed_fn=hashing_embed), tmp_path / "none")
    rec: dict = {}
    out = await scan(build_agent(scripted_model(rec)), frame, "water?", toolset=lookup_toolset(empty))
    assert rec["passages"] == [] and out.result.sources == []


async def test_router_offers_tool_only_on_tap_with_question(kb, frame, tmp_path):
    rec: dict = {}
    budget = Budget(tmp_path / "r.db", cap_usd=5.0)
    router = Router(budget, build_agent(scripted_model(rec)), "fake", toolset=lookup_toolset(kb))
    await router.run(frame, "water?", source="tap")
    assert rec["tools_seen"] == ["lookup"]
    rec.clear()
    await router.run(frame, "water?", source="live")
    assert rec["tools_seen"] == []
    rec.clear()
    await router.run(frame, None, source="tap")
    assert rec["tools_seen"] == []


async def test_tool_call_loop_is_cut_by_the_usage_limit(kb, frame):
    n = {"calls": 0}

    def loops(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        n["calls"] += 1
        return ModelResponse(parts=[ToolCallPart(tool_name="lookup", args={"query": "again"})])

    with pytest.raises(AgentFailed):
        await scan(build_agent(FunctionModel(loops, model_name="fake")), frame, "q", toolset=lookup_toolset(kb))
    assert n["calls"] <= 3
