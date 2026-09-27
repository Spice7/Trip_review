import pytest

from config import validate_config
from storage import read_json
from test_collector import FakeClient, config, raw_review
from test_shared_search import setup


class Counted(FakeClient):
    def details(self, identity):
        result = super().details(identity)
        result["traveler_ratings"]["overall"]["count"] = {
            "1": 1, "2": None, "3": 0, "4": 20, "5": 100}[identity]
        return result

    def reviews(self, identity, name=None):
        self.budget.reserve(1)
        self.calls.append(("review", identity))
        return {"data": [] if identity == "1" else [raw_review(identity)]}


def cfg(**overrides):
    return {**config(), "review_priority": "review_count", **overrides}


def test_reviews_ordered_by_count_after_bounded_details(tmp_path):
    _, _, client, collector = setup(tmp_path, cfg(), Counted)
    result = collector.run()
    assert [i for kind, i in client.calls if kind == "review"] == ["5", "4", "1", "2"]
    assert [i for kind, i in client.calls if kind == "detail"] == ["1", "2", "3", "4", "5"]
    assert result["stored_reviews"] == 3
    records = {r["location_id"]: r for r in read_json(tmp_path / "reviews.json")}
    assert records["1"]["review_status"] == "empty"
    assert records["3"]["review_status"] == "no_site_reviews"
    _, _, resumed, second = setup(tmp_path, cfg(), Counted)
    second.run()
    assert resumed.calls == []


def test_target_and_resume_keep_prefetched_details(tmp_path):
    _, _, first, collector = setup(tmp_path, cfg(target_reviewed_locations=1), Counted)
    collector.run()
    assert [c for c in first.calls if c[0] == "review"] == [("review", "5")]
    _, _, repeated, collector2 = setup(tmp_path, cfg(target_reviewed_locations=1), Counted)
    collector2.run()
    assert repeated.calls == []  # Cached success meets target before any new work.
    _, _, increased, collector3 = setup(tmp_path, cfg(target_reviewed_locations=2), Counted)
    collector3.run()
    assert increased.calls == [("review", "4")]


def test_preparation_leaves_room_for_review_and_resumes(tmp_path):
    budget, cache, client, collector = setup(tmp_path, cfg(), Counted)
    budget.limit_run(4)  # Search + two details + one review.
    result = collector.run()
    assert result["stopped"] == "budget" and budget.estimated_used == 4
    assert client.calls == [("search", 1), ("detail", "1"), ("detail", "2"), ("review", "1")]
    assert cache.get("locations", "2") is not None
    _, _, resumed, next_run = setup(tmp_path, cfg(), Counted)
    next_run.run()
    assert ("detail", "2") not in resumed.calls and ("review", "1") not in resumed.calls


def test_duplicates_across_regions_still_call_once(tmp_path):
    conf = {**config(second=True), "review_priority": "review_count", "search_strategy": "shared"}
    _, _, client, collector = setup(tmp_path, conf, Counted)
    collector.run()
    assert len([c for c in client.calls if c[0] == "detail"]) == 5
    assert len([c for c in client.calls if c[0] == "review"]) == 4


def test_detail_cache_survives_interrupt_before_reviews(tmp_path):
    class Interrupt(Counted):
        def details(self, identity):
            if identity == "3":
                raise KeyboardInterrupt
            return super().details(identity)
    _, cache, _, collector = setup(tmp_path, cfg(), Interrupt)
    assert collector.run()["stopped"] == "interrupted"
    assert cache.get("locations", "1") and cache.get("locations", "2")
    _, _, client, collector2 = setup(tmp_path, cfg(), Counted)
    collector2.run()
    assert ("detail", "1") not in client.calls and ("detail", "2") not in client.calls


def test_priority_is_optional_and_validated():
    assert validate_config(config())["review_priority"] == "search_order"
    assert validate_config(cfg())["review_priority"] == "review_count"
    with pytest.raises(ValueError):
        validate_config(cfg(review_priority="invalid"))
