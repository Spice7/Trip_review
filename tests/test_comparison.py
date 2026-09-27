import pytest

from budget import EntityBudgetManager
from diagnostics import comparison_plan, run_diagnostics
from storage import write_json
from test_collector import Response, Session
from test_diagnostics import classified, diagnostic_config
from tripadvisor_client import TripadvisorClient


def test_comparison_requests_bounds_and_preservation(tmp_path):
    cfg = diagnostic_config()
    write_json(tmp_path / "usage.json", {"estimated_used": 0})
    write_json(tmp_path / "reviews.json", [{"location_id": "1", "reviews": [], "review_count": 2}])
    before = (tmp_path / "reviews.json").read_bytes()
    plan = comparison_plan(cfg, tmp_path)
    assert (plan["max_calls"], plan["max_entities"], plan["retries"]) == (5, 17, 0)
    session = Session([
        Response({"data": [{"location": classified(2, "Accommodation")}]}),
        Response({"data": []}), Response({"data": []}),
        Response({"data": []}), Response({"data": [{"id": "99", "text": "PRIVATE"}]}),
    ])
    client = TripadvisorClient("offline", EntityBudgetManager(tmp_path / "usage.json"),
                               session=session, sleep=lambda _: None, max_attempts=1)
    path, report = run_diagnostics(plan, cfg, client, tmp_path)
    assert client.budget.estimated_used == 17
    assert report["findings"][0]["finding"] == "category_mismatch"
    assert report["findings"][1]["finding"] == "empty_inconclusive"
    assert report["findings"][-1]["returned_count"] == 1
    assert "PRIVATE" not in path.read_text(encoding="utf-8")
    assert (tmp_path / "reviews.json").read_bytes() == before
    assert not (tmp_path / "cache").exists()
    # Fake Session stores URL and keyword arguments, just as requests receives them.
    assert session.calls[0][0].endswith("/locations/nearby")
    assert session.calls[0][1]["params"]["include_photo"] == "false"
    assert session.calls[-2][1]["params"]["language"] == "primary"
    assert "language" not in session.calls[-1][1]["params"]


def test_alternative_offset_rejected_before_spend(tmp_path):
    budget = EntityBudgetManager(tmp_path / "usage.json")
    client = TripadvisorClient("offline", budget, session=Session([]))
    with pytest.raises(ValueError, match="offset"):
        client.nearby(diagnostic_config()["regions"][0], "HOTEL", 10, "부산", source="locations")
    assert budget.estimated_used == 0
