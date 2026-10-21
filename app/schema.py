"""Typed contracts between the vision agent and everything else (constitution I).

Why this file exists: the model never returns free text. Pydantic AI is handed
`ScanResult` as its `output_type`, so the model's answer is parsed and
validated against these models before our code ever sees it. If the model
produces garbage (confidence 1.4, unknown category, missing label), validation
fails and Pydantic AI retries with the error message - we never get a
half-valid object. The same models serialize straight to JSON for the PWA.

Design choices:
- `Literal` enums for category/position/mood: a closed vocabulary keeps the UI
  simple and makes evals (feature 006) comparable across model changes.
- Everything about a person is an *estimate with its own confidence*
  (constitution IV). We never report age or mood as a fact.
- `bbox` is optional: vision models vary a lot in box quality; v1 shows cards,
  overlays come in feature 003.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Category = Literal[
    "person",
    "animal",
    "furniture",
    "electronics",
    "food",
    "plant",
    "vehicle",
    "tool",
    "text",
    "other",
]

Position = Literal[
    "standing",
    "sitting",
    "lying",
    "walking",
    "on_table",
    "on_floor",
    "on_wall",
    "held",
    "unknown",
]

Mood = Literal["happy", "neutral", "sad", "angry", "surprised", "tired", "unknown"]

AgeRange = Literal["child", "teen", "young_adult", "adult", "senior", "unknown"]


class BBox(BaseModel):
    """Normalized bounding box, 0..1, origin top-left (x, y) with width/height."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _inside_frame(self) -> "BBox":
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValueError("bbox extends outside the frame")
        return self


class MoodEstimate(BaseModel):
    """A guess plus how sure the model is about it (constitution IV). Closed vocabulary."""

    value: Mood
    confidence: float = Field(ge=0, le=1)


class AgeEstimate(BaseModel):
    value: AgeRange
    confidence: float = Field(ge=0, le=1)


class PersonDetails(BaseModel):
    """Only present when category == 'person'. Always estimates, never facts."""

    mood: MoodEstimate | None = None
    age_range: AgeEstimate | None = None


class DetectedObject(BaseModel):
    label: str = Field(min_length=1, max_length=60, description="Short name, e.g. 'coffee mug'")
    category: Category
    position: Position = "unknown"
    confidence: float = Field(ge=0, le=1, description="How sure the model is that this object is present")
    bbox: BBox | None = None
    person: PersonDetails | None = None
    answer: str | None = Field(default=None, description="Answer to the user's question, if it concerns this object")

    @model_validator(mode="after")
    def _person_details_only_for_people(self) -> "DetectedObject":
        if self.person is not None and self.category != "person":
            raise ValueError("person details are only allowed when category == 'person'")
        return self


class ScanResult(BaseModel):
    """What the agent returns for one frame. An empty list means 'nothing identifiable'."""

    objects: list[DetectedObject] = Field(default_factory=list)
    scene: str = Field(max_length=200, description="One-sentence description of the scene")
    answer: str | None = Field(default=None, description="Answer to the user's question when it is about the whole scene")

    @property
    def is_empty(self) -> bool:
        return not self.objects


class ScanResponse(BaseModel):
    """What `POST /scan` returns: the agent's result plus what the call cost us.

    `cached=True` means the gate matched a recent frame and no model was called;
    cost is then 0 and `request_id` is the id of the original paid scan.
    """

    result: ScanResult
    cached: bool
    request_id: str
    model: str
    cost_usd: float = Field(ge=0)
    latency_ms: int = Field(ge=0)
