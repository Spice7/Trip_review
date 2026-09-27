from budget import EntityBudgetManager
from cache_manager import CacheManager
from collector import Collector, plan
from spatial import box_area_upper, search_areas
from test_collector import FakeClient, config, raw_place


def region():
    return {"name": "큰 영역", "search_mode": "bbox", "lat": 35.1361, "lon": 129.0843999,
            "sw_lat": 35.0417291, "ne_lat": 35.1610786,
            "sw_lon": 129.0538951, "ne_lon": 129.2124731}


def test_partition_covers_original_box_without_overlap():
    original = region()
    tiles = search_areas(original)
    assert len(tiles) == 4
    assert all(box_area_upper(t) <= 49 for t in tiles)
    original_area = (original["ne_lat"] - original["sw_lat"]) * (original["ne_lon"] - original["sw_lon"])
    assert abs(sum((t["ne_lat"] - t["sw_lat"]) * (t["ne_lon"] - t["sw_lon"]) for t in tiles) - original_area) < 1e-10
    assert min(t["sw_lat"] for t in tiles) == original["sw_lat"]
    assert max(t["ne_lat"] for t in tiles) == original["ne_lat"]
    for i, a in enumerate(tiles):
        for b in tiles[i + 1:]:
            overlap_y = min(a["ne_lat"], b["ne_lat"]) - max(a["sw_lat"], b["sw_lat"])
            overlap_x = min(a["ne_lon"], b["ne_lon"]) - max(a["sw_lon"], b["sw_lon"])
            assert overlap_y <= 0 or overlap_x <= 0


def test_partition_shares_location_limit_and_resumes(tmp_path):
    class Sparse(FakeClient):
        def nearby(self, area, category, page, city):
            self.budget.reserve(5)
            tile = search_areas(region()).index(area)
            self.calls.append(("search", tile, page))
            return {"data": [{"location": raw_place(tile * 2 + i)} for i in (1, 2)],
                    "pagination": {"total_pages": 1}}
    cfg = config()
    cfg["regions"] = [region()]
    cache = CacheManager(tmp_path / "cache")
    budget = EntityBudgetManager(tmp_path / "entity_usage.json")
    expected = plan(cfg, cache, budget)
    assert expected["estimated_search_calls"] == 4
    client = Sparse(budget)
    result = Collector(cfg, cache, budget, client, tmp_path).run()
    assert result["locations"] == 5  # Not 5 per tile.
    assert len([c for c in client.calls if c[0] == "search"]) == 3
    assert budget.estimated_used <= expected["additional_entities_without_retries"]
    client2 = Sparse(budget)
    Collector(cfg, cache, budget, client2, tmp_path).run()
    assert client2.calls == []
    cfg["max_locations"] = 8
    result = Collector(cfg, cache, budget, client2, tmp_path).run()
    assert result["locations"] == 8
    assert [c for c in client2.calls if c[0] == "search"] == [("search", 3, 1)]
