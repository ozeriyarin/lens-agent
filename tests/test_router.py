"""Router tests (feature 002): the policy decision table and every edge case in spec 002.

Both tiers are FunctionModels so each test scripts exactly what "Gemini" and
"Haiku" reply, and the ledger is inspected to prove what was charged.
"""

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agent import AgentFailed, build_agent
from app.budget import Budget, BudgetExceeded, estimate_scan_cost
from app.gate import prepare
from app.router import Router, is_weak
from app.schema import DetectedObject, ScanResult
from tests.test_gate import make_scene

PRIMARY = "google:gemini-2.5-flash"
ESCALATION = "anthropic:claude-haiku-4-5"


def obj(label="mug", confidence=0.9, answer=None):
    return DetectedObject(label=label, category="other", confidence=confidence, answer=answer)


# --- policy ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result, question, weak",
    [
        (ScanResult(scene="blur", objects=[]), None, True),
        (ScanResult(scene="desk", objects=[obj(confidence=0.55)]), None, True),
        (ScanResult(scene="desk", objects=[obj(confidence=0.55), obj("pen", 0.61)]), None, False),  # best counts
        (ScanResult(scene="desk", objects=[obj()]), None, False),
        (ScanResult(scene="desk", objects=[obj()]), "is it full?", True),  # asked, nobody answered
        (ScanResult(scene="desk", objects=[obj(answer="half")]), "is it full?", False),
        (ScanResult(scene="desk", objects=[obj()], answer="yes"), "is it full?", False),
        (ScanResult(scene="desk", objects=[obj()]), "   ", False),  # blank question is no question
    ],
)
def test_is_weak_decision_table(result, question, weak):
    assert is_weak(result, question, threshold=0.6) is weak


# --- router --------------------------------------------------------------------------


def scripted(replies, name):
    """A FunctionModel that returns the next scripted reply; dicts become structured output,
    strings become plain text (= malformed for our schema), exceptions are raised."""
    calls = {"n": 0}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        reply = replies.pop(0)
        if isinstance(reply, str):
            return ModelResponse(parts=[TextPart(reply)])
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=reply)])

    return FunctionModel(fn, model_name=name), calls


WEAK = {"scene": "dark", "objects": []}
STRONG = {"scene": "desk", "objects": [{"label": "laptop", "category": "electronics", "confidence": 0.95}]}
BETTER = {"scene": "desk, dim", "objects": [{"label": "laptop", "category": "electronics", "confidence": 0.8}]}


@pytest.fixture
def frame():
    return prepare(make_scene(9))


def make_router(tmp_path, primary_replies, escalation_replies=None, cap=5.0, enabled=True):
    budget = Budget(tmp_path / "l.db", cap_usd=cap)
    p_model, p_calls = scripted(primary_replies, PRIMARY)
    e_model, e_calls = scripted(escalation_replies or [], ESCALATION)
    router = Router(
        budget,
        build_agent(p_model), PRIMARY,
        build_agent(e_model) if enabled else None, ESCALATION if enabled else "",
        escalate_below=0.6,
    )
    return router, budget, p_calls, e_calls


async def test_strong_primary_result_is_not_escalated(tmp_path, frame):
    router, budget, p, e = make_router(tmp_path, [STRONG], [BETTER])
    out = await router.run(frame)
    assert out.escalated is False and out.model == PRIMARY and out.primary_model == PRIMARY
    assert (p["n"], e["n"]) == (1, 0)
    assert [x.tier for x in budget.entries()] == ["primary"]
    assert out.cost_usd == pytest.approx(budget.month_to_date())


async def test_weak_primary_result_is_escalated_and_both_calls_billed(tmp_path, frame):
    router, budget, p, e = make_router(tmp_path, [WEAK], [BETTER])
    out = await router.run(frame)
    assert out.escalated is True and out.model == ESCALATION and out.primary_model == PRIMARY
    assert out.result.objects[0].label == "laptop"
    assert (p["n"], e["n"]) == (1, 1)
    entries = budget.entries()
    assert [(x.model, x.tier, x.status) for x in entries] == [
        (PRIMARY, "primary", "settled"), (ESCALATION, "escalation", "settled")]
    assert out.cost_usd == pytest.approx(sum(x.cost_usd for x in entries))
    assert out.request_id == entries[0].request_id  # cache key stays the primary id
    assert budget.escalations() == 1


async def test_unanswered_question_escalates(tmp_path, frame):
    router, budget, p, e = make_router(tmp_path, [STRONG], [{**STRONG, "answer": "about half"}])
    out = await router.run(frame, question="is the mug full?")
    assert out.escalated and out.result.answer == "about half"


async def test_escalation_disabled_means_001_behaviour(tmp_path, frame):
    router, budget, p, e = make_router(tmp_path, [WEAK], enabled=False)
    out = await router.run(frame)
    assert router.enabled is False
    assert out.escalated is False and out.result.is_empty and e["n"] == 0


async def test_escalation_refused_by_budget_returns_primary_result(tmp_path, frame):
    cap = estimate_scan_cost(PRIMARY) + estimate_scan_cost(ESCALATION) / 2  # room for one, not two
    router, budget, p, e = make_router(tmp_path, [WEAK], [BETTER], cap=cap)
    out = await router.run(frame)
    assert out.escalated is False and out.result.is_empty
    assert e["n"] == 0
    assert [x.tier for x in budget.entries()] == ["primary"]  # no escalation row at all


async def test_escalation_failure_keeps_primary_result_and_charges_the_attempt(tmp_path, frame):
    router, budget, p, e = make_router(tmp_path, [WEAK], ["nope", "still nope"])
    out = await router.run(frame)
    assert out.escalated is False and out.model == PRIMARY and out.result.is_empty
    assert e["n"] == 2  # tried, retried once, gave up
    entries = budget.entries()
    assert [(x.tier, x.status) for x in entries] == [("primary", "settled"), ("escalation", "failed")]
    assert out.cost_usd == pytest.approx(sum(x.cost_usd for x in entries))  # failed attempt is in the price


async def test_primary_failure_is_not_escalated(tmp_path, frame):
    router, budget, p, e = make_router(tmp_path, ["bad", "bad"], [BETTER])
    with pytest.raises(AgentFailed):
        await router.run(frame)
    assert e["n"] == 0
    assert [(x.tier, x.status) for x in budget.entries()] == [("primary", "failed")]


async def test_primary_refused_by_budget_raises_before_any_call(tmp_path, frame):
    router, budget, p, e = make_router(tmp_path, [STRONG], [BETTER], cap=0.0001)
    with pytest.raises(BudgetExceeded):
        await router.run(frame)
    assert (p["n"], e["n"]) == (0, 0) and budget.entries() == []
