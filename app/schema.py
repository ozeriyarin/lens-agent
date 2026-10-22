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

from datetime import datetime
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
    """Normalized bounding box, 0..1, origin top-left (x, y) with width/height.

    The field descriptions are part of the JSON schema the model sees. Vision models
    have other habits (Gemini: [ymin, xmin, ymax, xmax] on 0..1000), so each field says
    exactly what it is; vague descriptions produced boxes in the wrong place.
    """

    x: float = Field(ge=0, le=1, description="LEFT edge as a fraction of image width (0 = left edge, 1 = right edge)")
    y: float = Field(ge=0, le=1, description="TOP edge as a fraction of image height (0 = top edge, 1 = bottom edge)")
    w: float = Field(gt=0, le=1, description="WIDTH as a fraction of image width (right edge = x + w). Not the right-edge coordinate.")
    h: float = Field(gt=0, le=1, description="HEIGHT as a fraction of image height (bottom edge = y + h). Not the bottom-edge coordinate.")

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
    # Model-facing box. Gemini was trained to localize as [ymin, xmin, ymax, xmax] on 0..1000;
    # asking for that format gives much tighter boxes than our own x/y/w/h convention did.
    # It is converted to `bbox` on validation and never serialized to clients.
    box_2d: list[int] | None = Field(
        default=None,
        exclude=True,
        min_length=4,
        max_length=4,
        description="Tight bounding box as [ymin, xmin, ymax, xmax], integers on a 0..1000 scale "
                    "where 0,0 is the top-left of the image. Prefer this over bbox.",
    )
    bbox: BBox | None = Field(default=None, description="Alternative to box_2d: normalized x,y,w,h. Leave empty if box_2d is given.")
    person: PersonDetails | None = None
    answer: str | None = Field(default=None, description="Answer to the user's question, if it concerns this object")
    # Feature 004: what makes THIS object different from others of its kind. Embedded as text
    # and matched against remembered entities. Never for people (constitution IV).
    identity: str | None = Field(
        default=None,
        max_length=160,
        description="Distinguishing details of this specific object: colour, material, brand, pattern, "
                    "visible text, damage. E.g. 'white ceramic mug, blue rim, chipped handle'. "
                    "Omit for people.",
    )

    @model_validator(mode="after")
    def _person_details_only_for_people(self) -> "DetectedObject":
        if self.person is not None and self.category != "person":
            raise ValueError("person details are only allowed when category == 'person'")
        if self.category == "person":
            self.identity = None  # people are never remembered as identities
        return self

    @model_validator(mode="after")
    def _box_2d_to_bbox(self) -> "DetectedObject":
        if self.box_2d is None:
            return self
        ymin, xmin, ymax, xmax = self.box_2d
        if not all(0 <= v <= 1000 for v in self.box_2d):
            raise ValueError("box_2d values must be within 0..1000")
        if ymax <= ymin or xmax <= xmin:
            raise ValueError("box_2d must satisfy ymin < ymax and xmin < xmax")
        if self.bbox is None:
            self.bbox = BBox(x=xmin / 1000, y=ymin / 1000, w=(xmax - xmin) / 1000, h=(ymax - ymin) / 1000)
        return self


class Source(BaseModel):
    """A note passage the answer relied on (feature 005). Only ever values returned by `lookup`."""

    file: str = Field(max_length=200)
    heading: str = Field(max_length=200)


class ScanResult(BaseModel):
    """What the agent returns for one frame. An empty list means 'nothing identifiable'."""

    objects: list[DetectedObject] = Field(default_factory=list)
    scene: str = Field(max_length=200, description="One-sentence description of the scene")
    answer: str | None = Field(default=None, description="Answer to the user's question when it is about the whole scene")
    sources: list[Source] = Field(
        default_factory=list,
        description="Passages from the owner's notes (via lookup) that the answer relies on; empty when none",
    )

    @property
    def is_empty(self) -> bool:
        return not self.objects


class EntityRef(BaseModel):
    """What memory (feature 004) knows about one detected object, aligned with result.objects."""

    id: str
    known: bool = Field(description="True when this detection was linked to an entity seen before")
    sightings: int = Field(ge=1)
    first_seen: datetime
    last_seen: datetime
    identity: str


class ScanResponse(BaseModel):
    """What `POST /scan` returns: the agent's result plus what the call cost us.

    `cached=True` means the gate matched a recent frame and no model was called;
    cost is then 0 and `request_id` is the id of the original paid scan.
    `escalated=True` (feature 002) means the primary model's result was weak and
    `result` comes from `model`, the escalation tier; `cost_usd` then covers both calls.
    `entities` (feature 004) has one entry per object in `result.objects`, None where
    memory could not or did not link it (people, no identity, memory off).
    """

    result: ScanResult
    cached: bool
    request_id: str
    model: str
    primary_model: str
    escalated: bool = False
    cost_usd: float = Field(ge=0)
    latency_ms: int = Field(ge=0)
    entities: list[EntityRef | None] = Field(default_factory=list)
