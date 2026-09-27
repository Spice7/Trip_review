"""Resolve explicit Busan region names using Nominatim, with persistent caching."""

import hashlib
import math
import time

import requests

from config import CITY, validate_config
from storage import StateError, read_json, write_json

ATTRIBUTION = "지역 검색: © OpenStreetMap contributors (ODbL), https://www.openstreetmap.org/copyright"
ALIASES = {"광안리": "광안리해수욕장"}


def normalize(value):
    return "".join(value.split()).casefold()


class BusanGeocoder:
    def __init__(self, root, endpoint="https://nominatim.openstreetmap.org/search", *,
                 session=None, sleep=time.sleep, clock=time.monotonic):
        self.root, self.endpoint = root, endpoint
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "BusanTripadvisorReviewCollector/0.2",
                                     "Accept": "application/json"})
        self.sleep, self.clock = sleep, clock
        self.last = None

    def close(self):
        self.session.close()

    def resolve(self, name):
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError("부산 세부 지역 이름을 1~100자로 입력하세요.")
        query_name = ALIASES.get(name, name)
        key = hashlib.sha256(f"busan-v1:{query_name}".encode()).hexdigest()
        path = self.root / f"{key}.json"
        saved = read_json(path)
        if saved is None:
            # Persist throttling across invocations under the main output lock.
            stamp = read_json(self.root / "last_request.json", {})
            wait = max(0, 1.1 - (time.time() - stamp.get("time", 0)))
            if self.last is not None:
                wait = max(wait, 1.1 - (self.clock() - self.last))
            self.sleep(min(wait, 1.1))
            write_json(self.root / "last_request.json", {"time": time.time()})
            self.last = self.clock()
            try:
                response = self.session.get(self.endpoint, params={
                    "q": f"부산광역시 {query_name}", "countrycodes": "kr",
                    "format": "jsonv2", "addressdetails": 1, "namedetails": 1,
                    "accept-language": "ko", "limit": 10,
                }, timeout=(10, 20), allow_redirects=False)
                try:
                    if response.status_code != 200:
                        raise ValueError(f"지역 검색 서비스 오류 HTTP {response.status_code}. 나중에 다시 실행하세요.")
                    rows = response.json()
                finally:
                    response.close()
            except requests.RequestException:
                raise ValueError("지역 검색 서비스에 연결할 수 없습니다. 저장된 설정을 사용하거나 나중에 다시 실행하세요.") from None
            if not isinstance(rows, list):
                raise ValueError("지역 검색 응답 형식이 올바르지 않습니다.")
            # Cache empty/ambiguous results too, avoiding repeated identical queries.
            saved = {"candidates": self.candidates(rows, query_name)}
            write_json(path, saved)
        if (not isinstance(saved, dict) or not isinstance(saved.get("candidates"), list)
                or not all(isinstance(c, dict) and isinstance(c.get("display_name"), str)
                           for c in saved["candidates"])):
            raise StateError(f"지역 검색 캐시가 손상되었습니다: {path}")
        choices = saved["candidates"]
        if not choices:
            raise ValueError(f"'{name}'을 부산 내 지역으로 확인할 수 없습니다. 정식 지역명으로 다시 입력하세요.")
        if len(choices) != 1:
            labels = " / ".join(c["display_name"] for c in choices)
            raise ValueError(f"'{name}'의 후보가 여러 개입니다: {labels}. 구·동을 포함해 더 구체적으로 입력하세요.")
        region = {**choices[0], "name": name}
        return validate_config({"city": CITY, "regions": [region],
                                "categories": ["ATTRACTION"], "max_locations": 5})["regions"][0]

    @staticmethod
    def candidates(rows, query_name):
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            address = row.get("address") or {}
            if address.get("country_code") != "kr":
                continue
            busan = any(normalize(str(address.get(k, ""))) in
                        ("부산", "부산광역시", "busan", "busanmetropolitancity")
                        for k in ("city", "state", "province"))
            if not busan:
                continue
            names = [row.get("name", ""), *(row.get("namedetails") or {}).values()]
            # Permit qualified names such as '수영구 광안동', but require the actual place name.
            if not any(normalize(query_name) == normalize(str(n)) or
                       query_name.split()[-1] == str(n) for n in names if n):
                continue
            if row.get("category", row.get("class")) not in ("boundary", "place", "natural", "leisure"):
                continue  # A shop/hotel bearing the district's name is not that region.
            identity = (row.get("osm_type"), row.get("osm_id"))
            if identity in seen:
                continue
            try:
                lat, lon = float(row["lat"]), float(row["lon"])
                south, north, west, east = map(float, row["boundingbox"])
                if not all(math.isfinite(x) for x in (lat, lon, south, north, west, east)):
                    continue
                if not (-90 <= south <= lat <= north <= 90 and -180 <= west <= lon <= east <= 180):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            region = {"name": query_name, "lat": lat, "lon": lon,
                      "source": "OpenStreetMap/Nominatim", "display_name": row.get("display_name", query_name),
                      "osm_type": identity[0], "osm_id": identity[1]}
            if row.get("type") == "administrative" and south < north and west < east:
                region.update(sw_lat=south, sw_lon=west, ne_lat=north, ne_lon=east,
                              search_mode="bbox")
            else:
                region.update(radius_km=1.0, search_mode="radius")
            result.append(region)
            seen.add(identity)
        return result
