"""Agent tests (constitution V) without any network.

Pydantic AI ships two fake models: `TestModel` fills the output schema with
canned values, and `FunctionModel` lets us script exactly what the "model"
replies. That is enough to test everything except the quality of the real
model's answers, which is what the eval set (feature 006) is for.
"""

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from app.agent import INSTRUCTIONS, AgentFailed, build_agent, scan
from app.gate import prepare
from app.schema import ScanResult
from tests.test_gate import make_scene

DESK = {
    "scene": "a desk with a laptop and a mug",
    "objects": [
        {"label": "laptop", "category": "electronics", "position": "on_table", "confidence": 0.95,
         "bbox": {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.4}},
        {"label": "coffee mug", "category": "other", "position": "on_table", "confidence": 0.8,
         "answer": "It looks empty."},
    ],
}


@pytest.fixture
def frame():
    return prepare(make_scene(1))


async def test_scan_returns_typed_result_and_usage(frame):
    agent = build_agent(TestModel(custom_output_args=DESK))
    outcome = await scan(agent, frame, question="is the mug full?")
    assert isinstance(outcome.result, ScanResult)
    assert [o.label for o in outcome.result.objects] == ["laptop", "coffee mug"]
    assert outcome.result.objects[1].answer == "It looks empty."
    assert outcome.model_name == "test"
    assert outcome.input_tokens > 0 and outcome.output_tokens > 0  # FR-6 needs real numbers
    assert outcome.latency_ms >= 0


async def test_question_and_image_both_reach_the_model(frame):
    seen: list[ModelMessage] = []

    def fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(messages)
        # Reply via the output tool, the way a real model does for structured output.
        from pydantic_ai.messages import ToolCallPart
        tool = info.output_tools[0]
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"scene": "ok", "objects": []})])

    outcome = await scan(build_agent(FunctionModel(fake)), frame, question="what colour is it?")
    assert outcome.result.is_empty
    request = seen[0]
    texts = [p for part in request.parts for p in (part.content if isinstance(part.content, list) else [part.content])]
    assert any(isinstance(t, str) and "what colour is it?" in t for t in texts)
    assert any(getattr(t, "media_type", None) == "image/jpeg" for t in texts)
    assert INSTRUCTIONS.split("\n")[0] in (request.instructions or "")


async def test_overlong_question_is_truncated_in_the_prompt(frame):
    from app.agent import MAX_QUESTION_CHARS, _prompt

    text = _prompt(frame, "q" * (MAX_QUESTION_CHARS + 1000))[0]
    assert len(text) < MAX_QUESTION_CHARS + 100


async def test_malformed_output_is_retried_once_then_fails(frame):
    calls = 0

    def always_garbage(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[TextPart("I'd rather chat than fill in your schema.")])

    with pytest.raises(AgentFailed):
        await scan(build_agent(FunctionModel(always_garbage)), frame)
    assert calls == 2  # first attempt + exactly one retry (spec edge case)


async def test_invalid_values_are_sent_back_for_retry_then_accepted(frame):
    attempts: list[dict] = [
        {"scene": "x", "objects": [{"label": "cat", "category": "animal", "confidence": 1.7}]},  # out of range
        {"scene": "x", "objects": [{"label": "cat", "category": "animal", "confidence": 0.7}]},
    ]

    def fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        from pydantic_ai.messages import ToolCallPart
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=attempts.pop(0))])

    outcome = await scan(build_agent(FunctionModel(fake)), frame)
    assert outcome.result.objects[0].confidence == 0.7
    assert attempts == []  # both scripted replies were consumed: validation error triggered the retry


async def test_live_frames_ask_for_brief_output(frame):
    from app.agent import LIVE_HINT, _prompt

    assert LIVE_HINT.strip() in _prompt(frame, None, brief=True)[0]
    assert LIVE_HINT.strip() not in _prompt(frame, None)[0]


def test_scan_settings_turn_thinking_off():
    from pydantic_ai.models.test import TestModel

    from app.agent import SCAN_MODEL_SETTINGS, build_agent

    assert SCAN_MODEL_SETTINGS["thinking"] is False
    assert build_agent(TestModel()).model_settings == SCAN_MODEL_SETTINGS
