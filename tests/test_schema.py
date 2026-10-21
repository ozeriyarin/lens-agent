"""Schema tests (constitution V). The schema is a contract, so we test the
boundaries: what must be rejected, not only what is accepted."""

import pytest
from pydantic import ValidationError

from app.schema import BBox, DetectedObject, MoodEstimate, PersonDetails, ScanResult


def test_minimal_object_is_valid():
    obj = DetectedObject(label="mug", category="furniture", confidence=0.9)
    assert obj.position == "unknown"
    assert obj.bbox is None and obj.person is None


def test_confidence_must_be_between_0_and_1():
    with pytest.raises(ValidationError):
        DetectedObject(label="mug", category="other", confidence=1.4)
    with pytest.raises(ValidationError):
        DetectedObject(label="mug", category="other", confidence=-0.1)


def test_unknown_category_is_rejected():
    with pytest.raises(ValidationError):
        DetectedObject(label="mug", category="kitchenware", confidence=0.5)


def test_empty_label_is_rejected():
    with pytest.raises(ValidationError):
        DetectedObject(label="", category="other", confidence=0.5)


def test_bbox_must_stay_inside_frame():
    BBox(x=0.5, y=0.5, w=0.5, h=0.5)  # touches the edge: fine
    with pytest.raises(ValidationError):
        BBox(x=0.6, y=0.0, w=0.5, h=0.5)
    with pytest.raises(ValidationError):
        BBox(x=0.0, y=0.0, w=0.0, h=0.5)  # zero width


def test_person_details_require_person_category():
    details = PersonDetails(mood=MoodEstimate(value="happy", confidence=0.6))
    DetectedObject(label="man", category="person", confidence=0.9, person=details)
    with pytest.raises(ValidationError):
        DetectedObject(label="dog", category="animal", confidence=0.9, person=details)


def test_person_estimates_use_closed_vocabularies():
    # Free text here would let model output reach the UI unvalidated and break evals.
    with pytest.raises(ValidationError):
        MoodEstimate(value="<img onerror=alert(1)>", confidence=0.5)
    with pytest.raises(ValidationError):
        PersonDetails.model_validate({"age_range": {"value": "about 30", "confidence": 0.5}})


def test_scan_result_empty_means_nothing_identifiable():
    result = ScanResult(objects=[], scene="blurry wall")
    assert result.is_empty


def test_scan_result_round_trips_through_json():
    result = ScanResult(
        scene="desk with a laptop and a mug",
        objects=[
            DetectedObject(label="laptop", category="electronics", position="on_table", confidence=0.95,
                           bbox=BBox(x=0.1, y=0.2, w=0.5, h=0.4)),
            DetectedObject(label="mug", category="other", position="on_table", confidence=0.8),
        ],
    )
    restored = ScanResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert [o.label for o in restored.objects] == ["laptop", "mug"]


def test_json_schema_is_exportable():
    # Pydantic AI sends this schema to the model as the required output shape.
    schema = ScanResult.model_json_schema()
    assert "objects" in schema["properties"]
    assert schema["$defs"]["DetectedObject"]["properties"]["category"]["enum"][0] == "person"


def test_box_2d_is_converted_to_bbox_and_not_serialized():
    # Gemini's native format: [ymin, xmin, ymax, xmax] on 0..1000.
    obj = DetectedObject(label="mug", category="other", confidence=0.9, box_2d=[100, 200, 500, 600])
    assert obj.bbox is not None
    assert (obj.bbox.x, obj.bbox.y, obj.bbox.w, obj.bbox.h) == (0.2, 0.1, 0.4, 0.4)
    dumped = obj.model_dump()
    assert "box_2d" not in dumped and dumped["bbox"]["x"] == 0.2
    assert "box_2d" in DetectedObject.model_json_schema()["properties"]  # the model still sees it


def test_box_2d_rejects_inverted_or_out_of_range_boxes():
    with pytest.raises(ValidationError):
        DetectedObject(label="mug", category="other", confidence=0.9, box_2d=[500, 200, 100, 600])  # ymax < ymin
    with pytest.raises(ValidationError):
        DetectedObject(label="mug", category="other", confidence=0.9, box_2d=[0, 0, 1200, 500])  # > 1000
    with pytest.raises(ValidationError):
        DetectedObject(label="mug", category="other", confidence=0.9, box_2d=[0, 0, 500])  # 3 values


def test_explicit_bbox_wins_over_box_2d():
    obj = DetectedObject(label="mug", category="other", confidence=0.9, box_2d=[0, 0, 1000, 1000],
                         bbox=BBox(x=0.1, y=0.1, w=0.2, h=0.2))
    assert obj.bbox.w == 0.2
