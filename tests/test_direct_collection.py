import logging

import pytest

import main as cli
from budget import EntityBudgetManager
from cache_manager import CacheManager
from config import Settings
from direct_collector import DirectCollector, direct_plan
from location_list import read_location_list, url_location_id
from storage import read_json, write_json
from tripadvisor_client import APIError, AuthenticationError
from tripadvisor_client import TripadvisorClient
from test_collector import Response, Session


def csv_file(tmp_path, rows):
    path = tmp_path / "ids.csv"
    path.write_text("region,category,location_id,name,tripadvisor_url\n" + rows, encoding="utf-8-sig")
    return path


def setup(tmp_path, rows="A,hotel,1,One,\nB,restaurant,2,Two,\n", limit=50):
    report = read_location_list(csv_file(tmp_path, rows))
    budget = EntityBudgetManager(tmp_path / "entity_usage.json", 100)
    budget.limit_run(limit)
    budget.save()
    return report, CacheManager(tmp_path / "cache"), budget


class Client:
    def __init__(self, budget, results):
        self.budget, self.results, self.calls = budget, results, []

    def reviews(self, identity, name):
        self.budget.reserve(1)
        self.calls.append(identity)
        result = self.results[identity]
        if isinstance(result, BaseException):
            raise result
        return {"data": result}


@pytest.mark.parametrize("kind", ["Restaurant", "Hotel", "Attraction"])
def test_url(kind):
    assert url_location_id(f"https://www.tripadvisor.co.kr/{kind}_Review-g1-d27961145-Reviews-X.html") == "27961145"


def test_validation_union_conflicts(tmp_path):
    url = "https://www.tripadvisor.com/Hotel_Review-g1-d1-Reviews-X.html"
    report = read_location_list(csv_file(tmp_path,
        f"A,hotel,1,One,{url}\nB,restaurant,,One,{url}\nC,hotel,2,Bad,{url}\n"
        "A,hotel,abc,Bad,\nA,bad,3,Bad,\n,hotel,4,Bad,\nA,hotel,5,,\n"))
    assert (report["valid_rows"], report["invalid_rows"]) == (2, 5)
    item, = report["locations"]
    assert item["regions"] == ["A", "B"] and item["category"] is None
    assert item["category_conflict"] == ["HOTEL", "RESTAURANT"]


def test_bad_header(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("id,name\n1,abc\n")
    with pytest.raises(ValueError):
        read_location_list(path)


def test_cache_empty_plan_and_budget(tmp_path):
    report, cache, budget = setup(tmp_path, limit=1)
    cache.put("reviews", "1", {"reviews": [], "fetched_at": "fixture", "review_status": "empty"})
    plan = direct_plan(report, cache, budget)
    assert plan["already_cached"] == 1 and plan["new_review_requests_required"] == 1
    assert direct_plan(report, cache, budget, True)["maximum_api_attempts_this_run"] == 1
    client = Client(budget, {"2": [{"id": "20", "rating": 5}]})
    result = DirectCollector(report, cache, budget, client, tmp_path).run()
    assert client.calls == ["2"] and result["cached_locations"] == 1
    assert result["new_reviews_collected"] == 1
    assert DirectCollector(report, cache, budget, client, tmp_path).run()["new_review_api_calls"] == 0


def test_merge_preserves_existing_and_deduplicates(tmp_path):
    report, cache, budget = setup(tmp_path)
    old = {"location_id": "1", "name": "Old", "category": "HOTEL", "rating": 4,
           "regions": ["Old region"], "reviews": [{"review_id": "10", "rating": 4}], "extra": 99}
    other = {**old, "location_id": "99"}
    write_json(tmp_path / "reviews.json", [old, other])
    client = Client(budget, {"1": [{"id": "10"}, {"id": "11"}], "2": []})
    stats = DirectCollector(report, cache, budget, client, tmp_path).run()
    records = {p["location_id"]: p for p in read_json(tmp_path / "reviews.json")}
    assert records["99"] == other
    assert records["1"]["name"] == "Old" and records["1"]["extra"] == 99
    assert records["1"]["regions"] == ["Old region", "A"]
    assert len(records["1"]["reviews"]) == 2 and stats["new_reviews_collected"] == 1
    client.results["1"] = []
    DirectCollector(report, cache, budget, client, tmp_path, True).run()
    assert len(read_json(tmp_path / "reviews.json")[0]["reviews"]) == 2


@pytest.mark.parametrize("status", [400, 404, 500])
def test_http_error_not_cached_and_continue(tmp_path, status):
    report, cache, budget = setup(tmp_path)
    client = Client(budget, {"1": APIError("fixture", status=status), "2": []})
    result = DirectCollector(report, cache, budget, client, tmp_path).run()
    assert client.calls == ["1", "2"] and result["empty_review_responses"] == 1
    assert cache.get("reviews", "1") is None
    first, second = read_json(tmp_path / "reviews.json")
    assert first["review_status"] == "http_error" and first["error_status"] == status
    assert second["review_status"] == "empty"


def test_auth_and_budget_stop(tmp_path):
    report, cache, budget = setup(tmp_path, limit=1)
    client = Client(budget, {"1": AuthenticationError("fixture"), "2": []})
    assert DirectCollector(report, cache, budget, client, tmp_path).run()["stopped"] == "authentication"
    assert client.calls == ["1"]
    budget.limit_run(1)
    client.results["1"] = []
    assert DirectCollector(report, cache, budget, client, tmp_path).run()["stopped"] == "budget"
    assert client.calls == ["1", "1"]


def test_dry_run_no_client_no_ledger_or_results_change(tmp_path, monkeypatch):
    report, cache, budget = setup(tmp_path)
    before = budget.path.read_bytes()
    monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(cli.Settings, "load", lambda: Settings("offline-fixture", 100))
    monkeypatch.setattr(cli, "setup_logging", lambda *a: logging.getLogger("test"))
    def forbidden(*a, **kw):
        pytest.fail("No client or geocoder allowed")
    monkeypatch.setattr(cli, "TripadvisorClient", forbidden)
    monkeypatch.setattr(cli, "BusanGeocoder", forbidden)
    assert cli.main(["--location-list", report["input_file"], "--dry-run"]) == 0
    assert budget.path.read_bytes() == before
    assert not (tmp_path / "reviews.json").exists()


def test_interruption_resume(tmp_path):
    report, cache, budget = setup(tmp_path)
    client = Client(budget, {"1": [{"id": "10"}], "2": KeyboardInterrupt()})
    assert DirectCollector(report, cache, budget, client, tmp_path).run()["stopped"] == "interrupted"
    client.results["2"] = []
    DirectCollector(report, cache, budget, client, tmp_path).run()
    assert client.calls == ["1", "2", "2"]


def test_exact_v1_requests_no_retry(tmp_path):
    report, cache, budget = setup(tmp_path)
    session = Session([Response({}, 500), Response({"data": []})])
    client = TripadvisorClient("offline-fixture", budget, session=session, sleep=lambda _: None)
    DirectCollector(report, cache, budget, client, tmp_path).run()
    assert len(session.calls) == 2
    for identity, (url, kwargs) in zip(("1", "2"), session.calls):
        assert url.endswith(f"/api/locations/{identity}/reviews")
        assert kwargs["params"] == {"version": "1", "language": "primary", "page": 1,
                                     "size": 3, "sort_by": "MOST_RECENT"}


def test_ledger_failure_stops_before_http(tmp_path, monkeypatch):
    report, cache, budget = setup(tmp_path)
    before = budget.path.read_bytes()
    session = Session([Response({"data": []})])
    client = TripadvisorClient("offline-fixture", budget, session=session)
    def fail():
        raise PermissionError("fixture ledger locked")
    monkeypatch.setattr(budget, "save", fail)
    with pytest.raises(PermissionError):
        DirectCollector(report, cache, budget, client, tmp_path).run()
    assert session.calls == [] and budget.path.read_bytes() == before
    assert read_json(tmp_path / "direct_collection_stats.json")["stopped"] == "storage_error"


def test_cli_confirmation_decline(tmp_path, monkeypatch):
    report, cache, budget = setup(tmp_path)
    before = budget.path.read_bytes()
    monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(cli.Settings, "load", lambda: Settings("offline-fixture", 100))
    monkeypatch.setattr(cli, "setup_logging", lambda *a: logging.getLogger("test"))
    monkeypatch.setattr("builtins.input", lambda _: "n")
    monkeypatch.setattr(cli, "TripadvisorClient", lambda *a, **kw: pytest.fail("Declined"))
    assert cli.main(["--location-list", report["input_file"]]) == 0
    assert budget.path.read_bytes() == before
