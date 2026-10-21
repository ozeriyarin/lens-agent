"""The vision agent: image in, validated `ScanResult` out (constitution I).

Why Pydantic AI here: we hand it `output_type=ScanResult` and it does the
part that is easy to get wrong by hand. It turns the Pydantic model into a
JSON schema the provider understands, asks the model to fill it, validates
the reply, and if validation fails it sends the error back to the model for
one retry. Our code only ever sees a valid `ScanResult` or an exception.

What this module deliberately does *not* do: read env vars, touch the
budget, or cache. `main.py` composes gate -> budget -> agent so each piece
stays testable on its own. Model choice comes in as a string
(`google-gla:gemini-2.5-flash`, `anthropic:claude-haiku-4-5`) or a Model
object (tests pass `TestModel`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pydantic_ai import Agent, BinaryContent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models import Model

from app.gate import PreparedFrame
from app.schema import ScanResult

INSTRUCTIONS = """\
You are a careful vision assistant. You receive one photo from a phone camera and
return a structured list of the objects you can actually see.

Rules:
- One entry per distinct physical object. Use a short everyday label ("coffee mug",
  "laptop", "golden retriever"). Pick the closest category from the schema.
- `position` describes how the object sits in the scene (on_table, on_floor, held,
  standing, ...). Use "unknown" rather than guessing.
- `confidence` is how sure you are the object is present, 0..1. Be honest: a blurry
  or partly hidden object gets a lower number.
- `bbox` is optional. Only include it when you can place the object reasonably well,
  as fractions of the image width/height from the top-left corner.
- For a person, fill `person.mood` and `person.age_range` as *estimates with a
  confidence*. Never state them as facts. Do not try to identify who the person is.
- `scene` is one plain sentence describing the whole picture.
- If the user asked a question: answer it in `answer` on the object it concerns,
  or in the top-level `answer` if it is about the whole scene. If you cannot tell
  from the image, say so briefly instead of inventing.
- If nothing is identifiable (blur, darkness, a blank wall), return an empty
  `objects` list and describe why in `scene`.
"""


class AgentFailed(RuntimeError):
    """The model could not produce a valid ScanResult even after the retry."""


@dataclass(frozen=True)
class ScanOutcome:
    """A validated result plus the numbers the budget ledger needs (FR-6)."""

    result: ScanResult
    model_name: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


def build_agent(model: str | Model) -> Agent[None, ScanResult]:
    """One agent per process. `retries={'output': 1}` = spec edge case: retry once, then fail."""
    return Agent(
        model,
        output_type=ScanResult,
        instructions=INSTRUCTIONS,
        retries={"output": 1, "tools": 1},
        name="lens-scan",
    )


def _prompt(frame: PreparedFrame, question: str | None) -> list[str | BinaryContent]:
    text = "Describe the objects in this photo."
    if question and question.strip():
        text += f" The user also asks: {question.strip()}"
    return [text, BinaryContent(data=frame.jpeg, media_type=frame.media_type)]


async def scan(agent: Agent[None, ScanResult], frame: PreparedFrame, question: str | None = None) -> ScanOutcome:
    """Run the agent on one prepared frame. Raises AgentFailed on malformed output."""
    started = time.perf_counter()
    try:
        run = await agent.run(_prompt(frame, question))
    except UnexpectedModelBehavior as e:
        raise AgentFailed(str(e)) from e
    usage = run.usage
    return ScanOutcome(
        result=run.output,
        model_name=getattr(agent.model, "model_name", str(agent.model)),
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
