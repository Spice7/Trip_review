import copy

import pytest

import main as cli
from cache_manager import CacheManager
from config import validate_config
from geocoder import BusanGeocoder
from storage import read_json, write_json
from test_collector import Response, Session
from budget import EntityBudgetManager
from tripadvisor_client import TripadvisorClient


def candidate(name="수영구", city="부산광역시", identity=1, kind="administrative"):
    # Synthetic geocoder fixture; these coordinates never enter production configuration.
    return {"name": name, "display_name": f"{name}, {city}, 대한민국",
            "address": {"city": city, "country_code": "kr"},
            "category": "boundary" if kind == "administrative" else "natural",
            "type": kind, "osm_type": "relation", "osm_id": identity,
            "lat": "35.1", "lon": "129.1", "boundingbox": ["35.09", "35.11", "129.09", "129.11"]}


def test_district_bbox_and_cache(tmp_path):
    session = Session([Response([candidate()])])
    geocoder = BusanGeocoder(tmp_path, session=session, sleep=lambda x: None)
    first = geocoder.resolve("수영구")
    assert first["search_mode"] == "bbox" and first["sw_lat"] == 35.09
    assert geocoder.resolve("수영구") == first
    assert len(session.calls) == 1
    assert session.calls[0][1]["params"]["q"] == "부산광역시 수영구"
    assert "X-API-Key" not in session.headers


def test_gwangalli_alias_uses_verified_beach_and_one_km(tmp_path):
    session = Session([Response([candidate("광안리해수욕장", kind="beach")])])
    result = BusanGeocoder(tmp_path, session=session, sleep=lambda x: None).resolve("광안리")
    assert result["radius_km"] == 1 and result["name"] == "광안리"
    assert result["lat"] == 35.1


def test_reject_other_city_same_district_and_cache_empty(tmp_path):
    session = Session([Response([candidate("남구", city="울산광역시")])])
    geocoder = BusanGeocoder(tmp_path, session=session, sleep=lambda x: None)
    for _ in range(2):
        with pytest.raises(ValueError, match="부산 내 지역"):
            geocoder.resolve("남구")
    assert len(session.calls) == 1


def test_ambiguous_response_does_not_pick_first(tmp_path):
    session = Session([Response([candidate(identity=1), candidate(identity=2)])])
    with pytest.raises(ValueError, match="여러 개"):
        BusanGeocoder(tmp_path, session=session, sleep=lambda x: None).resolve("수영구")


def test_reject_business_and_unrelated_name(tmp_path):
    shop = candidate()
    shop["category"] = "shop"
    session = Session([Response([shop, candidate("남구")])])
    with pytest.raises(ValueError, match="확인할 수 없습니다"):
        BusanGeocoder(tmp_path, session=session, sleep=lambda x: None).resolve("수영구")


def test_rate_limit_and_no_automatic_retry(tmp_path):
    session = Session([Response([candidate()]), Response(status=429)])
    delays = []
    geocoder = BusanGeocoder(tmp_path, session=session, sleep=delays.append, clock=lambda: 0)
    geocoder.resolve("수영구")
    with pytest.raises(ValueError, match="429"):
        geocoder.resolve("남구")
    assert delays[-1] >= 1.1
    assert len(session.calls) == 2


def test_bbox_request_parameters(tmp_path):
    geocoder = BusanGeocoder(tmp_path / "geo", session=Session([Response([candidate()])]), sleep=lambda x: None)
    region = geocoder.resolve("수영구")
    session = Session([Response({"data": []})])
    client = TripadvisorClient("offline", EntityBudgetManager(tmp_path / "entity_usage.json"), session=session)
    client.nearby(region, "RESTAURANT", 1, "부산")
    params = session.calls[0][1]["params"]
    assert params["sort"] == "rating,desc"
    assert "radius" not in params and "lat" not in params and "sw_lat" in params


def test_interactive_never_asks_city_or_coordinates(tmp_path, monkeypatch):
    replies = iter(["광안리, 수영구, 남구", "3", "5"])
    prompts = []
    def answer(prompt):
        prompts.append(prompt)
        return next(replies)
    monkeypatch.setattr("builtins.input", answer)
    session = Session([Response([candidate("광안리해수욕장", kind="beach")]),
                       Response([candidate()]), Response([candidate("남구")])])
    resolver = BusanGeocoder(tmp_path, session=session, sleep=lambda x: None)
    cfg = cli.interactive_config({"city": "Paris"}, resolver)
    assert cfg["city"] == "부산" and len(cfg["regions"]) == 3
    assert not any(word in p for p in prompts for word in ("위도", "경도", "반경", "도시"))


def test_other_city_config_rejected_before_resolution():
    class Never:
        def resolve(self, name):
            pytest.fail("should reject before network")
    with pytest.raises(ValueError, match="부산"):
        cli.resolve_config({"city": "서울", "regions": ["남구"]}, Never())
    with pytest.raises(ValueError, match="부산"):
        validate_config({"city": "Paris"})


def test_metadata_does_not_invalidate_search_cache():
    area = {"name": "광안리", "lat": 35.1, "lon": 129.1, "radius_km": 1}
    old = CacheManager.search_key("부산", area, "RESTAURANT")
    area.update(source="OpenStreetMap/Nominatim", search_mode="radius", display_name="광안리")
    assert CacheManager.search_key("부산", area, "RESTAURANT") == old


def test_named_config_dry_run_with_mocked_geocoding_only(tmp_path, monkeypatch, capsys):
    path = tmp_path / "names.json"
    write_json(path, {"city": "부산", "regions": ["광안리", "수영구", "남구"],
                      "categories": ["RESTAURANT"], "max_locations": 5})
    session = Session([Response([candidate("광안리해수욕장", kind="beach")]),
                       Response([candidate()]), Response([candidate("남구")])])
    monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(cli, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cli, "BusanGeocoder", lambda root, url: BusanGeocoder(
        root, url, session=session, sleep=lambda x: None))
    def blocked(*a, **kw):
        pytest.fail("Terra must not be constructed")
    monkeypatch.setattr(cli, "TripadvisorClient", blocked)
    assert cli.main(["--dry-run", "--config", str(path)]) == 0
    saved = read_json(tmp_path / "output/search_config.json")
    assert len(saved["regions"]) == 3
    assert not (tmp_path / "output/entity_usage.json").exists()
    assert '"additional_entities_without_retries": 45' in capsys.readouterr().out
    before = copy.deepcopy(session.calls)
    assert cli.main(["--dry-run", "--config", str(path)]) == 0
    assert session.calls == before
