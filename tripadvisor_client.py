import logging
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from config import API_VERSION, MAX_ATTEMPTS, PAGE_SIZE
from models import SchemaError
from spatial import box_area_upper

log = logging.getLogger("collector")
BASE_URL = "https://terra.tripadvisor.com/api"


class APIError(RuntimeError):
    def __init__(self, message, *, status=None, detail=None):
        super().__init__(message)
        self.status, self.detail = status, detail


class AuthenticationError(APIError):
    pass


def retry_delay(header, attempt):
    if header:
        try:
            seconds = float(header)
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                seconds = (when - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 2 ** attempt
        if seconds >= 0 and seconds != float("inf"):
            return seconds
    return float(2 ** attempt)


class TripadvisorClient:
    def __init__(self, key, budget, *, session=None, sleep=time.sleep, clock=time.monotonic,
                 max_attempts=MAX_ATTEMPTS):
        self.budget = budget
        self.session = session or requests.Session()
        self.session.headers.update({"X-API-Key": key, "Accept": "application/json"})
        self.sleep, self.clock = sleep, clock
        self.last_request = None
        if not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise ValueError("Invalid max_attempts")
        self.max_attempts = max_attempts
        self._key = key

    def safe_error(self, response):
        try:
            body = response.json()
        except ValueError:
            return "JSON 오류 설명 없음"
        if not isinstance(body, dict):
            return "오류 설명 없음"
        fields = {k: body[k] for k in ("title", "detail", "message", "status")
                  if isinstance(body.get(k), (str, int))}
        text = json.dumps(fields, ensure_ascii=False)
        if self._key:
            text = text.replace(self._key, "[REDACTED]")
        text = re.sub(r"(?i)(api[_-]?key|authorization|token)([\s\"':=]+)[^\s,\"}]+",
                      r"\1\2[REDACTED]", text)
        return text[:2000]

    def close(self):
        self.session.close()

    def get(self, endpoint, params, *, context="", attempts=None):
        """Strict endpoint allowlist prevents photos, multi-GET and redirects."""
        search = endpoint in ("/catalog/locations/nearby", "/locations/nearby")
        if search:
            if params.get("size") != PAGE_SIZE:
                raise ValueError("Search page size must remain fixed")
            # Account-specific Dashboard observation: one estimate unit per HTTP attempt.
            # This is not a universal Tripadvisor pricing rule.
            cost = 1
        elif re.fullmatch(r"/locations/[0-9]+(?:/reviews)?", endpoint):
            cost = 1
        else:
            raise ValueError("허용하지 않은 API endpoint입니다.")
        attempt_limit = self.max_attempts if attempts is None else attempts
        for attempt in range(attempt_limit):
            # Pace ALL requests; this also covers the stricter search limit.
            if self.last_request is not None:
                self.sleep(max(0, 1.1 - (self.clock() - self.last_request)))
            self.budget.reserve(cost)
            log.info("[Entity] %s / %s [API] %s %s attempt=%s",
                     self.budget.estimated_used, self.budget.hard_limit, endpoint,
                     context, attempt + 1)
            self.last_request = self.clock()
            response = None
            try:
                response = self.session.get(
                    BASE_URL + endpoint, params={"version": API_VERSION, **params},
                    timeout=(10, 30), allow_redirects=False,
                )
            except requests.RequestException:
                # Never log exception bodies/URLs/headers: these can echo credentials.
                log.warning("Network error endpoint=%s attempt=%s", endpoint, attempt + 1)
            if response is not None:
                status = response.status_code
                log.info("[HTTP] %s [API] %s", status, endpoint)
                if status == 200:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise SchemaError("API가 JSON이 아닌 응답을 반환했습니다.") from exc
                    finally:
                        response.close()
                    return data
                header = response.headers.get("Retry-After")
                detail = self.safe_error(response)
                response.close()
                log.warning("[API error] status=%s endpoint=%s detail=%s", status, endpoint, detail)
                if status == 401:
                    raise AuthenticationError("HTTP 401: .env의 API Key를 확인하세요.")
                if status != 429 and not 500 <= status <= 599:
                    hint = ("요청 파라미터/검색 범위를 확인하세요." if status == 400
                            else "접근 권한/대상 ID를 확인하세요." if status in (403, 404)
                            else "예상하지 못한 HTTP 응답입니다.")
                    raise APIError(f"HTTP {status}: endpoint={endpoint}; {hint} {detail}",
                                   status=status, detail=detail)
            else:
                header = None
            if attempt == attempt_limit - 1:
                raise APIError(f"최대 시도 {attempt_limit}회 실패: {endpoint}",
                               status=response.status_code if response is not None else None)
            delay = retry_delay(header, attempt + 1)
            # Never retry earlier than Retry-After. Long waits defer to the next run.
            if delay > 60:
                raise APIError("Retry-After가 60초를 초과합니다. 나중에 다시 실행하세요.")
            log.warning("[Retry] wait=%.1fs endpoint=%s", delay, endpoint)
            self.sleep(delay)

    def nearby(self, region, category, page, city, *, source="catalog"):
        if source not in ("catalog", "locations"):
            raise ValueError("Unknown search source")
        if source == "locations" and PAGE_SIZE * (page - 1) > 40:
            raise ValueError("Locations nearby offset exceeds 40")
        if region.get("search_mode") == "bbox" and box_area_upper(region) > 50:
            raise APIError("검색 사각형이 50㎢를 초과합니다. 분할 검색이 필요합니다.", status=400)
        area = ({k: region[k] for k in ("sw_lat", "sw_lon", "ne_lat", "ne_lon")}
                if region.get("search_mode") == "bbox" else
                {"lat": region["lat"], "lon": region["lon"],
                 "radius": region["radius_km"], "unit": "KM"})
        endpoint = "/catalog/locations/nearby" if source == "catalog" else "/locations/nearby"
        return self.get(endpoint, {
            **({"include_photo": "false"} if source == "locations" else {}),
            **area, "category": category,
            "page": page, "size": PAGE_SIZE,
            "sort": "rating,desc" if region.get("search_mode") == "bbox" else "distance,asc",
            "locale": "ko-KR",
        }, context=f"[Region] {city} {region['name']} [Category] {category} page={page}")

    def details(self, location_id):
        return self.get(f"/locations/{location_id}", {"locale": "ko-KR"},
                        context=f"[Location ID] {location_id}")

    def reviews(self, location_id, name=None, *, language="primary"):
        return self.get(f"/locations/{location_id}/reviews", {
            "version": "1",
            **({"language": language} if language is not None else {}),
            "sort_by": "MOST_RECENT", "page": 1, "size": 3,
        }, context=f"[Location] {name} [Location ID] {location_id}", attempts=1)
