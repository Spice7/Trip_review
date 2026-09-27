import math
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
CITY = "부산"
CATEGORIES = ("ATTRACTION", "HOTEL", "RESTAURANT")
PAGE_SIZE = 5  # Stable across runs: increasing max_locations never shifts page boundaries.
MAX_ATTEMPTS = 3
API_VERSION = "1"


@dataclass(frozen=True)
class Settings:
    api_key: str
    hard_limit: int
    geocoding_url: str = "https://nominatim.openstreetmap.org/search"

    @classmethod
    def load(cls, path=BASE_DIR / ".env"):
        values = dotenv_values(path)  # Intentionally read the key only from .env.
        key = (values.get("TRIPADVISOR_API_KEY") or "").strip()
        limit = int(values.get("HARD_ENTITY_BUDGET") or "800")
        if limit < 1:
            raise ValueError("HARD_ENTITY_BUDGET은 양의 정수여야 합니다.")
        url = values.get("GEOCODING_URL") or "https://nominatim.openstreetmap.org/search"
        if not url.startswith("https://"):
            raise ValueError("GEOCODING_URL은 HTTPS 주소여야 합니다.")
        return cls(key, limit, url)


def validate_config(data):
    if not isinstance(data, dict):
        raise ValueError("설정의 city가 필요합니다.")
    city = data.get("city", CITY)
    if city not in (CITY, "부산광역시"):
        raise ValueError("부산 지역만 지원합니다. 기존 다른 도시 설정을 재사용할 수 없습니다.")
    city = CITY
    regions = data.get("regions")
    cats = data.get("categories")
    maximum = data.get("max_locations")
    strategy = data.get("search_strategy", "shared")
    priority = data.get("review_priority", "search_order")
    if priority not in ("search_order", "review_count"):
        raise ValueError("review_priority는 search_order 또는 review_count여야 합니다.")
    if strategy not in ("shared", "per_category"):
        raise ValueError("search_strategy는 shared 또는 per_category여야 합니다.")
    if not city or not isinstance(regions, list) or not regions:
        raise ValueError("도시와 하나 이상의 세부 지역이 필요합니다.")
    if not isinstance(cats, list) or not cats or any(c not in CATEGORIES for c in cats):
        raise ValueError("categories에는 ATTRACTION/HOTEL/RESTAURANT를 지정하세요.")
    if type(maximum) is not int or not 1 <= maximum <= 1000:
        raise ValueError("최대 장소 수는 1~1000 정수여야 합니다.")
    target = data.get("target_reviewed_locations")
    if target is not None and (type(target) is not int or not 1 <= target <= maximum):
        raise ValueError("target_reviewed_locations는 1~max_locations 정수여야 합니다.")
    validated = []
    for r in regions:
        if not isinstance(r, dict) or not isinstance(r.get("name"), str) or not r["name"].strip():
            raise ValueError("지역 name이 필요합니다.")
        bbox = r.get("search_mode") == "bbox"
        fields = (("lat", -90, 90), ("lon", -180, 180))
        fields += (("sw_lat", -90, 90), ("ne_lat", -90, 90),
                   ("sw_lon", -180, 180), ("ne_lon", -180, 180)) if bbox else (("radius_km", 0, 8),)
        for key, low, high in fields:
            value = r.get(key)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not low <= value <= high or (key == "radius_km" and value == 0)):
                raise ValueError(f"{r['name']}: {key} 범위를 확인하세요 ({low}~{high}).")
        if bbox and not (r["sw_lat"] < r["ne_lat"] and r["sw_lon"] < r["ne_lon"]):
            raise ValueError("잘못된 검색 영역입니다.")
        region = {k: r[k] for k in ("name", *(f[0] for f in fields))}
        for k in ("search_mode", "source", "display_name", "osm_type", "osm_id"):
            if k in r:
                region[k] = r[k]
        region["name"] = region["name"].strip()
        if region["name"] in [x["name"] for x in validated]:
            raise ValueError("중복 지역 이름이 있습니다.")
        validated.append(region)
    return {"city": city, "regions": validated,
            "search_strategy": strategy,
            "review_priority": priority,
            "categories": list(dict.fromkeys(cats)), "max_locations": maximum,
            **({"target_reviewed_locations": target} if target is not None else {})}
