"""Eval scorer and runner tests (feature 006). The scorer is tested on hand-built cases;
the runner, bootstrap and compare end to end with the fake model and generated images."""

import json

import pytest
from pydantic_ai.models.test import TestModel

from app.config import Settings
from app.evals import (
    EvalItem,
    Expected,
    bootstrap,
    box_to_bbox,
    compare,
    iou,
    label_matches,
    load_set,
    match,
    run,
    score_photo,
    scorable,
    summarize,
)
from app.schema import BBox, DetectedObject, ScanResult
from tests.test_gate import make_scene


def obj(label, category="other", confidence=0.9, bbox=None, answer=None):
    return DetectedObject(label=label, category=category, confidence=confidence, bbox=bbox, answer=answer)


# --- matching ------------------------------------------------------------------------


@pytest.mark.parametrize("pred, name, ok", [
    ("coffee mug", "mug", True), ("Mug", "mug", True), ("mugshot", "mug", False),
    ("remote control", "remote", True), ("TV remote", "remote control", False), ("laptop", "", False),
    ("red  Coca-Cola can", "coca-cola", True),
])
def test_label_matches_whole_words_case_insensitive(pred, name, ok):
    assert label_matches(pred, name) is ok


def test_match_prefers_exact_then_contains_and_is_one_to_one():
    item = EvalItem(id="a", file="a.jpg", expect=[Expected("mug"), Expected("coffee mug")], forbid=["apple"])
    preds = [obj("coffee mug"), obj("mug"), obj("green apple")]
    pairs, forbid_hits, extras = match(preds, item)
    assert dict(pairs) == {0: 1, 1: 0}  # "mug" takes the literal "mug", "coffee mug" the other
    assert forbid_hits == ["apple"] and extras == [2]


def test_match_respects_category_and_aliases():
    item = EvalItem(id="a", file="a.jpg", expect=[Expected("person", aliases=["man", "woman"], category="person")])
    assert match([obj("man", category="person")], item)[0] == [(0, 0)]
    assert match([obj("man", category="other")], item)[0] == []  # right word, wrong category


def test_iou_and_box_conversion():
    a = BBox(x=0.0, y=0.0, w=0.5, h=0.5)
    assert iou(a, a) == pytest.approx(1.0)
    assert iou(a, BBox(x=0.5, y=0.5, w=0.5, h=0.5)) == 0.0
    assert iou(a, BBox(x=0.25, y=0.0, w=0.5, h=0.5)) == pytest.approx(0.25 / 0.75)
    b = box_to_bbox([100, 200, 600, 700])
    assert (b.x, b.y, b.w, b.h) == (0.2, 0.1, 0.5, 0.5)


# --- scoring --------------------------------------------------------------------------


def test_score_photo_recall_iou_forbid_and_answer():
    item = EvalItem(id="desk", file="d.jpg",
                    expect=[Expected("laptop", box=[200, 100, 600, 600]), Expected("mug"), Expected("pen")],
                    forbid=["cat"], question="is the mug full?", expect_answer_contains="empty")
    result = ScanResult(scene="desk", objects=[
        obj("laptop", "electronics", bbox=BBox(x=0.1, y=0.2, w=0.5, h=0.4)),
        obj("coffee mug", answer="It looks empty."), obj("cat", "animal")])
    s = score_photo(item, result, latency_ms=900, cost_usd=0.002, escalated=False, model="m")
    assert s.recall == pytest.approx(2 / 3) and s.missed == ["pen"]
    assert s.iou_mean == pytest.approx(1.0) and s.forbid_hits == ["cat"] and s.extras == ["cat"]
    assert s.answer_hit is True and s.error is None


def test_score_photo_without_result_is_a_total_miss():
    item = EvalItem(id="x", file="x.jpg", expect=[Expected("a"), Expected("b")])
    s = score_photo(item, None, error="boom")
    assert s.recall == 0.0 and s.missed == ["a", "b"] and s.error == "boom"


def test_summarize_aggregates():
    ok = [score_photo(EvalItem(id=f"p{i}", file="f", expect=[Expected("mug")]),
                      ScanResult(scene="s", objects=[obj("mug")] if i < 3 else []),
                      latency_ms=100 * (i + 1), cost_usd=0.001, escalated=(i == 0), model="m") for i in range(4)]
    bad = [score_photo(EvalItem(id="e", file="f", expect=[Expected("mug")]), None, error="missing")]
    s = summarize(ok + bad)
    assert s["photos"] == 5 and s["errors"] == 1
    assert s["mean_recall"] == pytest.approx((3 + 0 + 0) / 5)
    assert s["escalation_rate"] == pytest.approx(0.25) and s["total_cost_usd"] == pytest.approx(0.004)
    assert s["latency_p50_ms"] in (200, 300) and s["latency_p95_ms"] == 400
    assert [f["id"] for f in s["failures"]] == ["p3", "e"]


# --- set file -------------------------------------------------------------------------


def test_load_set_parses_jsonl_skips_comments_and_drafts(tmp_path):
    p = tmp_path / "set.jsonl"
    p.write_text('# comment\n{"id":"a","file":"a.jpg","expect":[{"label":"mug","aliases":["cup"]}]}\n\n'
                 '{"id":"b","file":"b.jpg","draft":true}\n', encoding="utf-8")
    items = load_set(p)
    assert [i.id for i in items] == ["a", "b"] and items[0].expect[0].aliases == ["cup"]
    assert [i.id for i in scorable(items)] == ["a"]
    p.write_text('{"id":"a","file":"a.jpg"}\n{"id":"a","file":"b.jpg"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_set(p)


# --- runner, bootstrap, compare (offline) ---------------------------------------------------


def desk_model(labels=("laptop", "coffee mug")):
    return TestModel(custom_output_args={"scene": "a desk", "objects": [
        {"label": l, "category": "electronics" if l == "laptop" else "other", "confidence": 0.9,
         "box_2d": [200, 100, 600, 600] if l == "laptop" else None} for l in labels]})


@pytest.fixture
def photos(tmp_path):
    d = tmp_path / "photos"; d.mkdir()
    for i in range(3):
        (d / f"photo{i}.jpg").write_bytes(make_scene(i + 1))
    return d


def settings(tmp_path):
    return Settings(model="test", budget_usd=5.0, db_path=tmp_path / "unused.db", escalation_model="")


async def test_run_scores_the_set_and_writes_a_report(tmp_path, photos):
    set_path = tmp_path / "set.jsonl"
    set_path.write_text(
        '{"id":"p0","file":"photo0.jpg","expect":[{"label":"laptop","box":[200,100,600,600]},{"label":"mug","aliases":["cup"]}]}\n'
        '{"id":"p1","file":"photo1.jpg","expect":[{"label":"laptop"},{"label":"pen"}],"forbid":["cat"]}\n'
        '{"id":"gone","file":"missing.jpg","expect":[{"label":"laptop"}]}\n'
        '{"id":"d","file":"photo2.jpg","draft":true}\n', encoding="utf-8")
    report = await run(set_path=set_path, photos=photos, model=desk_model(), tag="t", settings=settings(tmp_path),
                       ledger=tmp_path / "ledger.db", runs_dir=tmp_path / "runs")
    s = report["summary"]
    assert report["set_size"] == 3 and s["photos"] == 3 and s["errors"] == 1  # draft skipped, missing file reported
    assert s["mean_recall"] == pytest.approx((1.0 + 0.5 + 0.0) / 3)
    assert s["iou_mean"] == pytest.approx(1.0) and s["iou_photos"] == 1
    assert [f["id"] for f in s["failures"]] == ["p1", "gone"]
    assert (tmp_path / "runs").glob("*-t.json") and json.loads(open(report["path"], encoding="utf-8").read())["fingerprint"]
    assert s["total_cost_usd"] > 0  # the fake model is still charged through the ledger


async def test_run_refuses_without_budget(tmp_path, photos):
    set_path = tmp_path / "set.jsonl"
    set_path.write_text('{"id":"p0","file":"photo0.jpg","expect":[{"label":"laptop"}]}\n', encoding="utf-8")
    tiny = Settings(model="test", budget_usd=0.0001, db_path=tmp_path / "x.db", escalation_model="")
    with pytest.raises(SystemExit):
        await run(set_path=set_path, photos=photos, model=desk_model(), settings=tiny, ledger=tmp_path / "l.db",
                  runs_dir=tmp_path / "runs", write=False)


async def test_bootstrap_appends_drafts_only_for_new_photos(tmp_path, photos):
    set_path = tmp_path / "set.jsonl"
    set_path.write_text('{"id":"p0","file":"photo0.jpg","expect":[{"label":"laptop"}]}\n', encoding="utf-8")
    new = await bootstrap(photos=photos, set_path=set_path, model=desk_model(), settings=settings(tmp_path), ledger=tmp_path / "l.db")
    assert [i.file for i in new] == ["photo1.jpg", "photo2.jpg"] and all(i.draft for i in new)
    assert new[0].expect[0].label == "laptop" and new[0].expect[0].box == [200, 100, 600, 600]
    items = load_set(set_path)
    assert len(items) == 3 and len(scorable(items)) == 1
    assert await bootstrap(photos=photos, set_path=set_path, model=desk_model(), settings=settings(tmp_path), ledger=tmp_path / "l.db") == []


def test_compare_prints_deltas():
    a = {"model": "google:x", "fingerprint": "aaa", "summary": {"mean_recall": 0.5, "forbid_hit_rate": 0.0, "iou_mean": None,
         "answer_accuracy": None, "escalation_rate": 0.1, "mean_cost_usd": 0.002, "latency_p50_ms": 1000, "latency_p95_ms": 2000, "photos": 7}}
    b = {**a, "fingerprint": "bbb", "summary": {**a["summary"], "mean_recall": 0.75, "latency_p50_ms": 900}}
    out = compare(a, b)
    assert "+0.2500" in out and "-100" in out and "mean_recall" in out


# --- feature 004: identity metrics -------------------------------------------------------------


def test_identity_rate_and_entity_links_per_photo():
    item = EvalItem(id="a", file="a.jpg", expect=[Expected("beer", aliases=["heineken"], entity="heineken"), Expected("mug")])
    result = ScanResult(scene="s", objects=[
        DetectedObject(label="Heineken bottle", category="food", confidence=0.9, identity="green 330 ml Heineken"),
        DetectedObject(label="mug", category="other", confidence=0.9),  # no identity
        DetectedObject(label="man", category="person", confidence=0.9)])
    s = score_photo(item, result, entity_ids=["ent-1", None, None])
    assert s.identity_rate == pytest.approx(0.5)  # 1 of 2 non-person objects
    assert s.entity_links == {"heineken": "ent-1"}


def test_identity_metrics_recall_and_precision():
    from app.evals import PhotoScore, identity_metrics

    def ps(pid, links):
        return PhotoScore(pid, "f", 1, 1, [], [], [], 1.0, None, None, 0, 0.0, False, "m", entity_links=links)

    scores = [ps("p1", {"heineken": "A", "mustard": "M"}), ps("p2", {"heineken": "A"}), ps("p3", {"heineken": "B", "mustard": "M"})]
    m = identity_metrics(scores)
    assert m["entity_groups"] == 2
    assert m["identity_recall"] == pytest.approx((1 + 0 + 0 + 1) / 4)  # heineken pairs: A-A ok, A-B no, A-B no; mustard M-M ok
    assert m["identity_precision"] == 1.0  # heineken ids never equal mustard ids
    assert identity_metrics([ps("x", {})]) == {"entity_groups": 0, "identity_recall": None, "identity_precision": None}


async def test_run_reports_identity_metrics_offline(tmp_path, photos):
    set_path = tmp_path / "set.jsonl"
    set_path.write_text(
        '{"id":"p0","file":"photo0.jpg","expect":[{"label":"laptop","entity":"lap"}]}\n'
        '{"id":"p1","file":"photo1.jpg","expect":[{"label":"laptop","entity":"lap"}]}\n', encoding="utf-8")
    model = TestModel(custom_output_args={"scene": "desk", "objects": [
        {"label": "laptop", "category": "electronics", "confidence": 0.9, "identity": "silver laptop with a blue sticker"}]})
    st = Settings(model="test", budget_usd=5.0, db_path=tmp_path / "u.db", escalation_model="", memory_threshold=0.65)
    report = await run(set_path=set_path, photos=photos, model=model, settings=st, ledger=tmp_path / "l.db",
                       runs_dir=tmp_path / "runs", write=False)
    s = report["summary"]
    assert s["identity_rate"] == 1.0 and s["entity_groups"] == 1 and s["identity_recall"] == 1.0
    assert any(p["entity_links"].get("lap") for p in report["photos"])
