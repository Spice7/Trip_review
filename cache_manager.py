import hashlib
import json
import logging
from pathlib import Path

from storage import StateError, read_json, write_json
from models import mark_legacy_category
from spatial import search_areas

log = logging.getLogger("collector")


class CacheManager:
    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def search_key(city, region, category):
        # Excludes max_locations, includes every query-affecting input and schema version.
        area = {k: v for k, v in region.items() if k in (
            "name", "lat", "lon", "radius_km", "sw_lat", "sw_lon", "ne_lat", "ne_lon")}
        data = {"city": city, "region": area, "category": category,
                "version": 1, "page_size": 5,
                "sort": "rating,desc" if region.get("search_mode") == "bbox" else "distance,asc",
                "locale": "ko-KR"}
        if len(search_areas(region)) > 1:
            data["area_partition"] = "bbox49-v1"
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def path(self, kind, key):
        if kind not in ("searches", "locations", "reviews") or not key.isalnum():
            raise ValueError("Invalid cache key")
        return self.root / kind / f"{key}.json"

    def get(self, kind, key):
        path = self.path(kind, key)
        value = read_json(path)
        if path.exists() and (not isinstance(value, dict) or value.get("schema") != 1):
            raise StateError("캐시 형식이 다릅니다. 원본을 보존하고 복구하세요.")
        if value is not None:
            valid = True
            if kind == "searches":
                valid = (isinstance(value.get("items"), list)
                         and type(value.get("next_page")) is int and value["next_page"] >= 1
                         and type(value.get("exhausted")) is bool
                         and all(isinstance(p, dict) and str(p.get("location_id", "")).isdigit()
                                 for p in value["items"]))
            elif kind == "locations":
                valid = (isinstance(value.get("location"), dict)
                         and value["location"].get("location_id") == key)
            elif kind == "reviews":
                valid = (isinstance(value.get("reviews"), list)
                         and len(value["reviews"]) <= 3
                         and all(isinstance(r, dict) for r in value["reviews"])
                         and isinstance(value.get("fetched_at"), str))
            if not valid:
                raise StateError(f"캐시 내용이 유효하지 않습니다: {path}")
            if kind == "locations":
                value["location"] = mark_legacy_category(value["location"])
            elif kind == "searches":
                value["items"] = [mark_legacy_category(item) for item in value["items"]]
        log.info("[Cache] %s %s %s", "miss" if value is None else "hit", kind, key)
        return value

    def put(self, kind, key, value):
        write_json(self.path(kind, key), {"schema": 1, **value})
