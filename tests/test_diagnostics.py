import json

import pytest

import main as cli
from budget import EntityBudgetManager
from cache_manager import CacheManager
from collector import Collector
from config import Settings
from diagnostics import cached_filter_conflicts, diagnostic_plan, run_diagnostics, summarize
from models import CategoryMismatch, location, response_metadata, search_page
from storage import read_json, write_json
from test_collector import FakeClient, Response, Session, config, raw_place
from tripadvisor_client import APIError, TripadvisorClient


def classified(identity, label):
    return {**raw_place(identity), "categories": [{"top_level_category": label}]}


def test_category_comes_from_api_not_query():
    place = location(classified(1, "Accommodation"), "ATTRACTION")
    assert place["category"] == "HOTEL"
    assert place["category_status"] == "verified"
    assert location(raw_place(1), "ATTRACTION")["category"] is None
    assert location(classified(1, "Experience"), "ATTRACTION")["category"] is None


def test_mismatched_search_not_accepted():
    with pytest.raises(CategoryMismatch):
        search_page({"data": [{"location": classified(1, "Accommodation")}]}, "ATTRACTION", 1, 5)


def test_mismatch_stops_before_any_detail_or_review(tmp_path):
    class Mismatch(FakeClient):
        def nearby(self, *args):
            self.budget.reserve(5)
            self.calls.append(("search", 1))
            return {"data": [{"location": classified(1, "Accommodation")}]}
    budget = EntityBudgetManager(tmp_path / "entity_usage.json")
    client = Mismatch(budget)
    result = Collector(config(), CacheManager(tmp_path / "cache"), budget, client, tmp_path).run()
    assert result["stopped"] == "category_mismatch"
    assert result["failures"] == 1 and client.calls == [("search", 1)]
    assert not list((tmp_path / "cache/searches").glob("*.json"))


def test_detail_mismatch_stops_before_review(tmp_path):
    class Mismatch(FakeClient):
        def details(self, identity):
            self.budget.reserve(1)
            return classified(identity, "Accommodation")
    budget = EntityBudgetManager(tmp_path / "entity_usage.json")
    client = Mismatch(budget)
    result = Collector(config(), CacheManager(tmp_path / "cache"), budget, client, tmp_path).run()
    assert result["stopped"] == "category_mismatch"
    assert budget.estimated_used == 6
    assert not any(c[0] == "review" for c in client.calls)
    assert read_json(tmp_path / "reviews.json")[0]["category"] == "HOTEL"


def test_old_cache_is_unverified_without_rewriting_files(tmp_path):
    cache = CacheManager(tmp_path / "cache")
    cache.put("locations", "1", {"location": {"location_id": "1", "category": "ATTRACTION"}})
    path = cache.path("locations", "1")
    before = path.read_bytes()
    place = cache.get("locations", "1")["location"]
    assert place["category"] is None and place["legacy_category"] == "ATTRACTION"
    assert place["category_status"] == "unverified_legacy"
    assert path.read_bytes() == before


def test_duplicate_category_cache_detection(tmp_path):
    cfg = config()
    cfg["categories"] = ["ATTRACTION", "HOTEL", "RESTAURANT"]
    cache = CacheManager(tmp_path / "cache")
    for category in cfg["categories"]:
        key = cache.search_key(cfg["city"], cfg["regions"][0], category)
        cache.put("searches", key, {"items": [{"location_id": "1", "category": category}],
                                     "next_page": 2, "exhausted": False})
    assert len(cached_filter_conflicts(cfg, cache)) == 2


def test_http400_records_safe_detail_and_does_not_retry(tmp_path, caplog):
    body = {"status": 400, "title": "Validation Error", "detail": "area too large; secret-KEY",
            "photos": [{"url": "DO_NOT_SAVE"}], "headers": {"Authorization": "DO_NOT_SAVE"}}
    session = Session([Response(body, 400)])
    client = TripadvisorClient("secret-KEY", EntityBudgetManager(tmp_path / "usage.json"),
                               session=session)
    with pytest.raises(APIError) as captured:
        client.reviews("1")
    message = str(captured.value)
    assert "area too large" in message and "allowlist" not in message
    assert "secret-KEY" not in message and "DO_NOT_SAVE" not in message
    assert "secret-KEY" not in caplog.text and "DO_NOT_SAVE" not in caplog.text
    assert captured.value.status == 400 and len(session.calls) == 1


def test_review_metadata_keeps_counts_without_content():
    raw = {"data": [], "pagination": {"page": 1, "size": 3, "total_elements": 0, "total_pages": 0},
           "language_meta": {"total_in_pool": 0, "matched_language": 0, "pool_definition": "recent_months"},
           "photos": ["DO_NOT_SAVE"]}
    meta = response_metadata(raw)
    assert meta["returned_count"] == 0 and meta["pagination"]["total_elements"] == 0
    assert meta["language_meta"]["pool_definition"] == "recent_months"
    assert "DO_NOT_SAVE" not in json.dumps(meta)


def diagnostic_config():
    cfg = config()
    cfg["regions"].append({"name": "bbox fixture", "lat": 35, "lon": 129,
                            "search_mode": "bbox", "sw_lat": 34.9, "ne_lat": 35.1,
                            "sw_lon": 128.9, "ne_lon": 129.1})
    return cfg


def test_diagnostic_plan_bound_and_preservation(tmp_path):
    cfg = diagnostic_config()
    write_json(tmp_path / "reviews.json", [{"location_id": "1", "review_count": 10, "reviews": []}])
    write_json(tmp_path / "entity_usage.json", {"estimated_used": 65})
    reviews_before = (tmp_path / "reviews.json").read_bytes()
    plan = diagnostic_plan(cfg, tmp_path)
    assert plan["max_calls"] == 4 and plan["max_entities"] == 16
    assert plan["retries"] == 0
    session = Session([Response({"data": [{"location": classified(1, "Accommodation")}]}),
                       Response({"data": [{"location": classified(1, "Accommodation")}]}),
                       Response({"detail": "box too large"}, 400),
                       Response({"data": [], "pagination": {"total_elements": 0}})])
    client = TripadvisorClient("offline", EntityBudgetManager(tmp_path / "entity_usage.json"),
                               session=session, sleep=lambda n: None, max_attempts=1)
    path, report = run_diagnostics(plan, cfg, client, tmp_path)
    assert client.budget.estimated_used == 81 and len(session.calls) == 4
    assert report["results"][2]["status"] == 400
    assert report["results"][0]["response"]["items"][0]["actual_categories"] == ["HOTEL"]
    assert path.exists() and (tmp_path / "reviews.json").read_bytes() == reviews_before
    assert not (tmp_path / "cache").exists()


def test_diagnostic_429_no_retries(tmp_path):
    cfg = config()
    plan = diagnostic_plan(cfg, tmp_path)
    session = Session([Response(status=429), Response(status=429)])
    client = TripadvisorClient("offline", EntityBudgetManager(tmp_path / "entity_usage.json"),
                               session=session, sleep=lambda n: None, max_attempts=1)
    run_diagnostics(plan, cfg, client, tmp_path)
    assert len(session.calls) == 2 and client.budget.estimated_used == 10


def test_diagnostic_summary_has_no_photos_or_reviewer():
    raw = {"data": [{"id": 10, "publish_ts": "date", "text": "DO_NOT_SAVE",
                     "photos": ["DO_NOT_SAVE"], "user": {"name": "DO_NOT_SAVE"}}]}
    assert "DO_NOT_SAVE" not in json.dumps(summarize(raw, "reviews"))


@pytest.mark.parametrize("dry", [True, False])
def test_diagnostic_dry_run_and_decline_never_connect(tmp_path, monkeypatch, dry):
    output = tmp_path / "output"
    write_json(output / "search_config.json", diagnostic_config())
    write_json(output / "entity_usage.json", {"estimated_used": 65})
    before = {p.name: p.read_bytes() for p in output.glob("*.json")}
    monkeypatch.setattr(cli, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cli, "OUTPUT_DIR", output)
    monkeypatch.setattr(cli.Settings, "load", lambda: Settings("offline", 800))
    def blocked(*args, **kwargs):
        pytest.fail("diagnostic dry run or decline must not construct a network client")
    monkeypatch.setattr(cli, "TripadvisorClient", blocked)
    monkeypatch.setattr(cli, "BusanGeocoder", blocked)
    monkeypatch.setattr("builtins.input", lambda p: "n")
    assert cli.main(["--diagnose"] + (["--dry-run"] if dry else [])) == 0
    assert before == {p.name: p.read_bytes() for p in output.glob("*.json")}


@pytest.mark.parametrize("refresh", [False, True])
def test_normal_collection_blocks_observed_filter_conflicts(tmp_path, monkeypatch, refresh):
    output = tmp_path / "output"
    cfg = config()
    cfg["categories"] = ["ATTRACTION", "HOTEL"]
    write_json(output / "search_config.json", cfg)
    write_json(output / "entity_usage.json", {"estimated_used": 65})
    cache = CacheManager(output / "cache")
    for category in cfg["categories"]:
        cache.put("searches", cache.search_key(cfg["city"], cfg["regions"][0], category),
                  {"items": [{"location_id": "1", "category": category}],
                   "next_page": 2, "exhausted": False})
    before = (output / "entity_usage.json").read_bytes()
    monkeypatch.setattr(cli, "OUTPUT_DIR", output)
    monkeypatch.setattr(cli, "BASE_DIR", tmp_path)
    def blocked(*args, **kwargs):
        pytest.fail("conflicting cached filters must not trigger paid collection")
    monkeypatch.setattr(cli, "TripadvisorClient", blocked)
    monkeypatch.setattr("builtins.input", blocked)
    args = ["--config", str(output / "search_config.json")]
    assert cli.main(args + (["--refresh"] if refresh else [])) == 2
    assert (output / "entity_usage.json").read_bytes() == before
