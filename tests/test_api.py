"""End-to-end tests of the HTTP layer with a fake model and a temp ledger.

These are the acceptance criteria from spec.md, automated: a scan returns
objects, a repeated frame is served from cache with no new ledger row, and a
reached budget refuses before any model call.
"""

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from app.budget import Budget, estimate_scan_cost
from app.config import Settings
from app.main import create_app
from tests.test_agent import DESK
from tests.test_gate import make_scene, recapture


def make_client(tmp_path, model=None, cap=5.0):
    settings = Settings(model="test:fake", budget_usd=cap, db_path=tmp_path / "ledger.db", cache_size=8)
    app = create_app(settings, model=model or TestModel(custom_output_args=DESK))
    return TestClient(app), settings


def post_scan(client, image: bytes, question: str | None = None):
    data = {"question": question} if question else {}
    return client.post("/scan", files={"image": ("frame.jpg", image, "image/jpeg")}, data=data)


def test_scan_returns_objects_and_costs_something(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        r = post_scan(client, make_scene(1), question="is the mug full?")
        assert r.status_code == 200, r.text
        body = r.json()
        assert [o["label"] for o in body["result"]["objects"]] == ["laptop", "coffee mug"]
        assert body["cached"] is False and body["cost_usd"] > 0 and body["model"] == "test"
        assert Budget(settings.db_path, 5).entries()[0].status == "settled"


def test_repeated_frame_is_free_and_adds_no_ledger_row(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        first = post_scan(client, make_scene(1)).json()
        again = post_scan(client, recapture(make_scene(1))).json()
        assert again["cached"] is True and again["cost_usd"] == 0
        assert again["request_id"] == first["request_id"]
        assert again["result"] == first["result"]
        assert len(Budget(settings.db_path, 5).entries()) == 1  # acceptance: duplicate scans add zero rows
        assert client.get("/stats").json()["cache_hits"] == 1


def test_budget_reached_refuses_without_calling_the_model(tmp_path):
    calls = 0

    def counting(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        from pydantic_ai.messages import ToolCallPart
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args={"scene": "x", "objects": []})])

    client, settings = make_client(tmp_path, model=FunctionModel(counting, model_name="fake"), cap=1.0)
    Budget(settings.db_path, 1.0).record(model="fake", input_tokens=0, output_tokens=0, latency_ms=0, cost_usd=0.999)
    with client:
        r = post_scan(client, make_scene(3))
        assert r.status_code == 402
        assert "monthly budget reached" in r.json()["detail"]
        assert calls == 0


def test_model_failure_keeps_the_estimate_charged(tmp_path):
    def garbage(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("nope")])

    client, settings = make_client(tmp_path, model=FunctionModel(garbage, model_name="fake"))
    with client:
        r = post_scan(client, make_scene(4))
        assert r.status_code == 502
        [entry] = Budget(settings.db_path, 5).entries()
        assert entry.status == "failed"
        # provider billed the attempt: charged at least the estimate, more if we saw more usage
        assert entry.cost_usd >= estimate_scan_cost("test:fake")


def test_overlong_question_is_rejected_before_any_work(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        r = post_scan(client, make_scene(6), question="x" * 501)
        assert r.status_code == 422
        assert Budget(settings.db_path, 5).entries() == []


def test_garbage_upload_is_a_400(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        assert post_scan(client, b"not an image").status_code == 400


def test_stats_reports_cap_and_spend(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        post_scan(client, make_scene(5))
        s = client.get("/stats").json()
        assert s["cap_usd"] == 5.0 and s["scans_this_month"] == 1
        assert s["month_to_date_usd"] == pytest.approx(s["recent"][0]["cost_usd"])
        assert s["remaining_usd"] == pytest.approx(5.0 - s["month_to_date_usd"])
        assert s["recent"][0]["status"] == "settled"


def test_token_guards_paid_endpoints_when_set(tmp_path):
    settings = Settings(model="test:fake", budget_usd=5.0, db_path=tmp_path / "l.db", scan_token="s3cret")
    client = TestClient(create_app(settings, model=TestModel(custom_output_args=DESK)))
    with client:
        assert client.get("/").status_code == 200  # the page itself is public; it holds no data
        assert client.get("/stats").status_code == 401
        assert post_scan(client, make_scene(7)).status_code == 401
        assert client.get("/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/stats", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        ok = client.post("/scan", files={"image": ("f.jpg", make_scene(7), "image/jpeg")},
                         headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert len(Budget(settings.db_path, 5).entries()) == 1  # the 401s cost nothing


def test_index_serves_the_pwa(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        r = client.get("/")
        assert r.status_code == 200 and "<html" in r.text.lower()


# --- feature 002 -------------------------------------------------------------------


def test_stats_has_per_model_breakdown_and_escalation_fields(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        post_scan(client, make_scene(8))
        s = client.get("/stats").json()
        assert s["escalation_model"] is None  # fake models never escalate
        assert s["escalations"] == 0 and s["scans_this_month"] == 1
        [row] = s["per_model"]
        assert row["model"] == "test" and row["calls"] == 1 and row["failed"] == 0
        assert row["cost_usd"] == pytest.approx(s["month_to_date_usd"])
        assert s["recent"][0]["tier"] == "primary"


def test_scan_response_reports_routing_fields(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        body = post_scan(client, make_scene(8)).json()
        assert body["primary_model"] == "test" and body["escalated"] is False
