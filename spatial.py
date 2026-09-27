"""Split large boxes without changing their outer bounds (server limit: 50 km²)."""

import math

EARTH_RADIUS_KM = 6371.0088
SAFE_BOX_KM2 = 49.0


def box_area_upper(region):
    south, north = region["sw_lat"], region["ne_lat"]
    west, east = region["sw_lon"], region["ne_lon"]
    nearest_equator = 0 if south <= 0 <= north else min(abs(south), abs(north))
    height = EARTH_RADIUS_KM * math.radians(north - south)
    width = EARTH_RADIUS_KM * math.radians(east - west) * math.cos(math.radians(nearest_equator))
    return height * width


def search_areas(region):
    if region.get("search_mode") != "bbox":
        return [region]
    pending, result = [dict(region)], []
    while pending:
        area = pending.pop()
        if box_area_upper(area) <= SAFE_BOX_KM2:
            result.append(area)
            continue
        if len(pending) + len(result) >= 255:
            raise ValueError("검색 범위가 너무 큽니다. 부산의 더 작은 세부 지역을 지정하세요.")
        latitude_span = area["ne_lat"] - area["sw_lat"]
        longitude_span = (area["ne_lon"] - area["sw_lon"]) * math.cos(math.radians(region["lat"]))
        if latitude_span >= longitude_span:
            middle = (area["sw_lat"] + area["ne_lat"]) / 2
            pending.extend([{**area, "ne_lat": middle}, {**area, "sw_lat": middle}])
        else:
            middle = (area["sw_lon"] + area["ne_lon"]) / 2
            pending.extend([{**area, "ne_lon": middle}, {**area, "sw_lon": middle}])
    # Search near the geocoder's representative point first; stop at the regional quota.
    return sorted(result, key=lambda a: (
        ((a["sw_lat"] + a["ne_lat"]) / 2 - region["lat"]) ** 2
        + (((a["sw_lon"] + a["ne_lon"]) / 2 - region["lon"])
           * math.cos(math.radians(region["lat"]))) ** 2))


def progress_for(state, region):
    areas = search_areas(region)
    progress = state.get("area_progress")
    if progress is None:
        progress = [{"next_page": state["next_page"], "exhausted": state["exhausted"]}]
        if len(areas) > 1:
            progress = [{"next_page": 1, "exhausted": False} for _ in areas]
    if (not isinstance(progress, list) or len(progress) != len(areas)
            or any(not isinstance(p, dict) or type(p.get("next_page")) is not int
                   or p["next_page"] < 1 or type(p.get("exhausted")) is not bool for p in progress)):
        raise ValueError("검색 구역 진행 정보가 올바르지 않습니다. 캐시를 보존하고 확인하세요.")
    return areas, progress
