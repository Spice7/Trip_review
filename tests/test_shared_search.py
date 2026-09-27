import pytest

from budget import BudgetExceeded, EntityBudgetManager
from cache_manager import CacheManager
from collector import Collector, plan
from config import Settings, validate_config
from models import location
from storage import read_json
from test_collector import FakeClient, config, raw_place
from test_diagnostics import classified


def setup(root, cfg, client_type=FakeClient):
    budget = EntityBudgetManager(root / "entity_usage.json")
    cache = CacheManager(root / "cache")
    client = client_type(budget)
    collector = Collector(cfg, cache, budget, client, root)
    return budget, cache, client, collector


def shared_config(maximum=5, second=False):
    return {**config(maximum, second), "search_strategy": "shared",
            "categories": ["ATTRACTION", "HOTEL", "RESTAURANT"]}


def test_one_search_stream_for_three_categories_and_resume(tmp_path):
    cfg = shared_config()
    budget, cache, client, collector = setup(tmp_path, cfg)
    estimates = plan(cfg, cache, budget)
    assert estimates["estimated_search_calls"] == 1
    assert estimates["maximum_candidate_locations"] == 5
    collector.run()
    assert len([c for c in client.calls if c[0] == "search"]) == 1
    assert budget.estimated_used == 11
    _, _, second, resumed = setup(tmp_path, cfg)
    resumed.run()
    assert second.calls == []


def test_other_category_candidates_reused_without_polluting_source_cache(tmp_path):
    cfg = shared_config(4)
    budget, cache, client, collector = setup(tmp_path, cfg)
    region = cfg["regions"][0]
    for category, ids in [("ATTRACTION", [1, 2, 3]), ("HOTEL", [3, 4])]:
        key = cache.search_key(cfg["city"], region, category)
        cache.put("searches", key, {"items": [location(raw_place(i), category) for i in ids],
                                     "next_page": 2, "exhausted": False})
    source_path = cache.path("searches", cache.search_key(cfg["city"], region, "ATTRACTION"))
    before = source_path.read_bytes()
    assert plan(cfg, cache, budget)["estimated_search_calls"] == 0
    result = collector.run()
    assert result["stored_reviews"] == 12
    assert not any(c[0] == "search" for c in client.calls)
    assert source_path.read_bytes() == before


def test_choose_most_advanced_stream_and_keep_cursor(tmp_path):
    cfg = shared_config(10)
    _, cache, client, collector = setup(tmp_path, cfg)
    key = cache.search_key(cfg["city"], cfg["regions"][0], "HOTEL")
    cache.put("searches", key, {"items": [location(raw_place(i), "HOTEL") for i in range(1, 6)],
                                 "next_page": 2, "exhausted": False})
    result = collector.run()
    assert [c for c in client.calls if c[0] == "search"] == [("search", 2)]
    assert result["stored_reviews"] == 30


def test_shared_search_does_not_reject_other_selected_actual_category(tmp_path):
    class Hotel(FakeClient):
        def details(self, identity):
            super().details(identity)
            return classified(identity, "Accommodation")
    _, _, client, collector = setup(tmp_path, shared_config(1), Hotel)
    result = collector.run()
    assert result["collection_stats"]["category_mismatch_locations"] == 0
    assert ("review", "1") in client.calls


def test_region_rotation_uses_disjoint_batches(tmp_path):
    class Regional(FakeClient):
        def nearby(self, region, category, page, city):
            self.budget.reserve(1)
            self.calls.append(("search", region["name"], page))
            offset = 100 if region["name"] == "Fixture B" else 0
            return {"data": [{"location": raw_place(i)} for i in
                             range(offset + (page-1)*5+1, offset + page*5+1)],
                    "pagination": {"total_pages": 2}}
    budget, _, client, collector = setup(tmp_path, shared_config(10, True), Regional)
    budget.limit_run(23)
    result = collector.run()
    assert [c for c in client.calls if c[0] == "search"][:2] == [
        ("search", "Fixture A", 1), ("search", "Fixture B", 1)]
    assert result["collection_stats"]["by_region"]["Fixture A"]["review_api_calls"] == 5
    assert result["collection_stats"]["by_region"]["Fixture B"]["review_api_calls"] == 5
    assert result["stopped"] == "budget" and budget.estimated_used == 23


def test_shared_target_counts_cached_reviews_across_categories(tmp_path):
    cfg = {**shared_config(10), "target_reviewed_locations": 2}
    _, _, client, collector = setup(tmp_path, cfg)
    collector.run()
    assert len([c for c in client.calls if c[0] == "review"]) == 2
    _, _, resumed, collector2 = setup(tmp_path, cfg)
    collector2.run()
    assert resumed.calls == []


def test_larger_cumulative_limit_and_small_run_budget(tmp_path):
    ledger = tmp_path / "entity_usage.json"
    budget = EntityBudgetManager(ledger, 1050)
    budget.reserve(1000)
    budget.limit_run(10)
    budget.reserve(10)
    with pytest.raises(BudgetExceeded):
        budget.reserve(1)
    assert read_json(ledger)["estimated_used"] == 1010
    assert read_json(ledger)["remaining_local_budget"] == 40
    assert EntityBudgetManager(ledger, 1050).remaining == 40
    env = tmp_path / "test.env"
    env.write_text("HARD_ENTITY_BUDGET=1050\nTRIPADVISOR_API_KEY=offline\n")
    assert Settings.load(env).hard_limit == 1050


def test_validated_old_config_defaults_to_shared():
    assert validate_config(config())["search_strategy"] == "shared"
    assert validate_config({**config(), "search_strategy": "per_category"})["search_strategy"] == "per_category"
