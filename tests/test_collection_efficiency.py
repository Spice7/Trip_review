import pytest
import requests

from budget import EntityBudgetManager
from cache_manager import CacheManager
from collector import Collector
from config import validate_config
from storage import read_json
from test_collector import FakeClient, Response, Session, config, raw_review
from test_diagnostics import classified
from tripadvisor_client import APIError, AuthenticationError, TripadvisorClient


class MixedClient(FakeClient):
    def details(self, identity):
        raw = super().details(identity)
        if identity == "1":
            raw["traveler_ratings"]["overall"]["count"] = 0
        elif identity == "2":
            raw["traveler_ratings"]["overall"]["count"] = None
        elif identity == "3":
            raw["traveler_ratings"]["overall"].pop("count")
        elif identity == "4":
            raw = classified(identity, "Accommodation")
        return raw

    def reviews(self, identity, name=None):
        self.budget.reserve(1)
        self.calls.append(("review", identity))
        counts = {"2": 0, "3": 2, "5": 3}
        return {"data": [raw_review(int(identity) * 10 + i) for i in range(counts[identity])]}


def execute(root, cfg, client_type=MixedClient, limit=800):
    budget = EntityBudgetManager(root / "entity_usage.json", limit)
    cache = CacheManager(root / "cache")
    client = client_type(budget)
    result = Collector(cfg, cache, budget, client, root).run()
    return budget, cache, client, result


def test_zero_unknown_empty_mismatch_dedup_and_stats(tmp_path):
    cfg = config(second=True)
    budget, cache, client, result = execute(tmp_path, cfg)
    assert result["stopped"] is None and result["failures"] == 0
    assert [i for kind, i in client.calls if kind == "review"] == ["2", "3", "5"]
    assert len([c for c in client.calls if c[0] == "detail"]) == 5
    assert cache.get("reviews", "1")["review_status"] == "no_site_reviews"
    assert cache.get("reviews", "2")["reviews"] == []
    assert cache.get("reviews", "2")["review_status"] == "empty"
    assert cache.get("reviews", "4") is None
    stats = result["collection_stats"]
    assert stats == read_json(tmp_path / "collection_stats.json")
    assert stats["candidate_locations"] == 10 and stats["unique_location_ids"] == 5
    assert stats["category_mismatch_locations"] == 1
    assert stats["no_site_reviews_locations"] == 1
    assert stats["review_api_calls"] == 3
    assert stats["locations_with_reviews"] == 2 and stats["empty_review_responses"] == 1
    assert stats["final_collected_reviews"] == 5
    assert stats["locations_success_rate_percent"] == pytest.approx(200 / 3)
    assert stats["reviews_per_review_api_call"] == pytest.approx(5 / 3)
    assert stats["by_region"]["Fixture A"]["reviews"] == 5
    assert stats["by_region"]["Fixture B"]["review_api_calls"] == 0
    assert stats["by_category"]["RESTAURANT"]["review_api_calls"] == 3
    before = budget.estimated_used
    budget2, _, client2, result2 = execute(tmp_path, cfg)
    assert client2.calls == [] and budget2.estimated_used == before
    assert result2["collection_stats"]["review_api_calls"] == 0
    assert result2["collection_stats"]["reviews_per_review_api_call"] == 0
    assert result2["collection_stats"]["final_collected_reviews"] == 5


def test_mismatch_then_matching_category_reuses_details(tmp_path):
    class Hotel(FakeClient):
        def details(self, identity):
            super().details(identity)
            return classified(identity, "Accommodation")
    cfg = config(maximum=1)
    cfg["categories"] = ["RESTAURANT", "HOTEL"]
    _, _, client, result = execute(tmp_path, cfg, Hotel)
    assert [c for c in client.calls if c[0] == "detail"] == [("detail", "1")]
    assert [c for c in client.calls if c[0] == "review"] == [("review", "1")]
    assert result["stored_reviews"] == 3


def test_target_uses_cache_and_resumes_unprocessed_page_items(tmp_path):
    cfg = {**config(10), "target_reviewed_locations": 2}
    _, _, client, result = execute(tmp_path, cfg, FakeClient)
    assert len([c for c in client.calls if c[0] == "review"]) == 2
    assert result["stored_reviews"] == 6
    _, _, cached, _ = execute(tmp_path, cfg, FakeClient)
    assert cached.calls == []
    cfg["target_reviewed_locations"] = 4
    _, _, resumed, result = execute(tmp_path, cfg, FakeClient)
    assert [c for c in resumed.calls if c[0] == "review"] == [("review", "3"), ("review", "4")]
    assert not any(c[0] == "search" for c in resumed.calls)
    assert result["stored_reviews"] == 12


def test_target_counts_only_reviewed_and_never_exceeds_maximum(tmp_path):
    cfg = {**config(5), "target_reviewed_locations": 3}
    _, _, client, result = execute(tmp_path, cfg)
    assert result["collection_stats"]["locations_with_reviews"] == 2
    assert all(str(identity) in {"1", "2", "3", "4", "5"} for _, identity in client.calls)


@pytest.mark.parametrize("failure", [Response({}, 429), Response({}, 500), requests.Timeout("offline")])
def test_review_transport_single_attempt_and_v1_params(tmp_path, failure):
    session = Session([failure, Response({"data": []})])
    budget = EntityBudgetManager(tmp_path / "entity_usage.json")
    client = TripadvisorClient("offline", budget, session=session)
    with pytest.raises(APIError):
        client.reviews("1")
    assert len(session.calls) == 1 and budget.estimated_used == 1
    assert session.calls[0][1]["params"] == {"version": "1", "language": "primary", "page": 1,
                                            "size": 3, "sort_by": "MOST_RECENT"}


def test_budget_block_is_not_counted_as_request(tmp_path):
    # Search 1 + first details 1 fills the budget before first review.
    budget, _, _, result = execute(tmp_path, config(), FakeClient, limit=2)
    assert result["stopped"] == "budget" and budget.estimated_used == 2
    assert result["collection_stats"]["review_api_calls"] == 0


@pytest.mark.parametrize("target", [0, True, 6, "2"])
def test_invalid_target_rejected(target):
    with pytest.raises(ValueError):
        validate_config({**config(), "target_reviewed_locations": target})


def test_legacy_empty_cache_remains_empty_without_request(tmp_path):
    budget = EntityBudgetManager(tmp_path / "entity_usage.json")
    budget.save()
    cache = CacheManager(tmp_path / "cache")
    cache.put("reviews", "1", {"reviews": [], "fetched_at": "old"})
    _, _, client, _ = execute(tmp_path, config(1), FakeClient)
    assert ("review", "1") not in client.calls
    assert read_json(tmp_path / "reviews.json")[0]["review_status"] == "empty"


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_each_return_count_is_cached_without_extra_calls(tmp_path, count):
    class CountClient(FakeClient):
        def reviews(self, identity, name=None):
            self.budget.reserve(1)
            self.calls.append(("review", identity))
            return {"data": [raw_review(i) for i in range(count)]}
    _, _, client, result = execute(tmp_path, config(1), CountClient)
    assert [c for c in client.calls if c[0] == "review"] == [("review", "1")]
    assert result["stored_reviews"] == count
    _, _, resumed, _ = execute(tmp_path, config(1), CountClient)
    assert resumed.calls == []


def test_authentication_still_stops_all_collection(tmp_path):
    class Unauthorized(FakeClient):
        def reviews(self, identity, name=None):
            self.budget.reserve(1)
            self.calls.append(("review", identity))
            raise AuthenticationError("HTTP 401")
    _, _, client, result = execute(tmp_path, config(second=True), Unauthorized)
    assert result["stopped"] == "authentication"
    assert client.calls == [("search", 1), ("detail", "1"), ("review", "1")]
    assert result["collection_stats"]["review_api_calls"] == 1
    assert result["collection_stats"]["locations_with_reviews"] == 0


def test_search_mismatch_skips_only_that_candidate(tmp_path):
    class MixedSearch(FakeClient):
        def nearby(self, *args):
            self.budget.reserve(1)
            self.calls.append(("search", 1))
            return {"data": [{"location": classified(1, "Accommodation")},
                             {"location": classified(2, "Eat & Drink")} ]}
    _, _, client, result = execute(tmp_path, config(2), MixedSearch)
    assert result["stopped"] is None
    assert client.calls == [("search", 1), ("detail", "2"), ("review", "2")]
    assert result["stored_reviews"] == 3
