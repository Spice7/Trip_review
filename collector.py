import logging
import math
from datetime import datetime, timezone

from budget import BudgetExceeded
from config import MAX_ATTEMPTS, PAGE_SIZE
from exporter import export
from models import (SchemaError, location, mark_legacy_category,
                    response_metadata, reviews, search_page)
from storage import read_json, write_json
from collection_stats import CollectionStats
from spatial import progress_for
from search_strategy import candidates, shared, sources
from tripadvisor_client import APIError, AuthenticationError

log = logging.getLogger("collector")


def blank_search():
    return {"items": [], "next_page": 1, "exhausted": False}


def plan(config, cache, budget, refresh=False):
    """Bound the actual algorithm's fixed page window; count uncached known IDs once."""
    page_limit = math.ceil(config["max_locations"] / PAGE_SIZE)
    search_calls = 0
    known = set()
    unknown = 0
    for region in config["regions"]:
        for category in sources(config, cache, region, refresh):
            key = cache.search_key(config["city"], region, category)
            state = blank_search() if refresh else (cache.get("searches", key) or blank_search())
            selected = candidates(config, cache, region, category, state, refresh)[:config["max_locations"]]
            known.update(x["location_id"] for x in selected)
            _, progress = progress_for(state, region)
            missing_pages = sum(0 if p["exhausted"] else max(
                0, page_limit - p["next_page"] + 1) for p in progress)
            if len(selected) >= config["max_locations"]:
                missing_pages = 0
            search_calls += missing_pages
            if missing_pages:
                unknown += max(0, config["max_locations"] - len(selected))
    detail_calls = unknown + sum(refresh or cache.get("locations", i) is None for i in known)
    review_calls = unknown + sum(refresh or cache.get("reviews", i) is None for i in known)
    entities = search_calls * PAGE_SIZE + detail_calls + review_calls
    return {
        "search_sources": {r["name"]: sources(config, cache, r, refresh) for r in config["regions"]},
        "limit_scope": "per_region" if shared(config) else "per_region_category",
        "maximum_candidate_locations": len(config["regions"]) * (1 if shared(config) else len(config["categories"]))
        * config["max_locations"],
        "estimated_search_calls": search_calls,
        "estimated_maximum_detail_calls": detail_calls,
        "estimated_maximum_review_calls": review_calls,
        "additional_entities_without_retries": entities,
        "additional_entities_with_all_retries": entities * MAX_ATTEMPTS,
        "current_accumulated_usage": budget.estimated_used,
        "estimated_total_without_retries": budget.estimated_used + entities,
        "hard_limit": budget.hard_limit, "remaining_budget": budget.remaining,
    }


class Collector:
    def __init__(self, config, cache, budget, client, output, refresh=False):
        self.config, self.cache, self.budget, self.client = config, cache, budget, client
        self.output, self.refresh = output, refresh
        self.records = {p["location_id"]: mark_legacy_category(p)
                        for p in read_json(output / "reviews.json", [])}
        self.attempted = set()
        self.detail_results = {}
        self.stats = CollectionStats()
        self.processed = 0
        self.failures = 0
        self.excel_ok = True
        self.failure_details = []

    def save(self):
        self.excel_ok = export(self.output, list(self.records.values()))
        self.budget.save()
        write_json(self.output / "collection_stats.json", self.stats.summary(self.records))

    def mismatch(self, record, region, category):
        self.stats.mismatches.add(record["location_id"])
        entry = {"region": region["name"], "category": category, "reason": "category_mismatch"}
        skipped = record.setdefault("skipped_searches", [])
        if entry not in skipped:
            skipped.append(entry)
        if not record.get("reviews") and record.get("review_status") not in ("empty", "no_site_reviews"):
            record["review_status"] = "category_mismatch"
        log.warning("[category_mismatch] id=%s requested=%s actual=%s; 해당 장소만 제외",
                    record["location_id"], category, record.get("actual_categories"))
        return False

    def collect_place(self, item, region, category):
        allowed = self.config["categories"] if shared(self.config) else [category]
        identity = item["location_id"]
        self.stats.candidates.add((region["name"], category, identity))
        self.stats.regions.setdefault(region["name"], dict.fromkeys(self.stats.api, 0))
        self.stats.categories.setdefault(category, dict.fromkeys(self.stats.api, 0))
        if identity not in self.records:
            self.records[identity] = {**item, "regions": [], "matched_regions": [],
                                      "reviews": [], "collected_review_count": 0,
                                      "review_status": "pending"}
        record = self.records[identity]
        region_label = f"{self.config['city']} {region['name']}"
        for key in ("regions", "matched_regions"):
            if region_label not in record[key]:
                record[key].append(region_label)
        if item.get("actual_categories") and not set(allowed).intersection(item["actual_categories"]):
            return self.mismatch(record, region, category)
        saved_details = None if self.refresh else self.cache.get("locations", identity)
        try:
            if identity in self.detail_results:
                details = self.detail_results[identity]
            elif saved_details is None:
                self.detail_results[identity] = {}  # Do not retry failed details within this run.
                details = location(self.client.details(identity), category)
                if details["location_id"] != identity:
                    raise SchemaError("요청한 ID와 상세 응답 ID가 다릅니다.")
                self.cache.put("locations", identity, {"location": details})
            else:
                details = saved_details["location"]
            self.detail_results[identity] = details
            if not details.get("actual_categories") and item.get("actual_categories"):
                details = {**details, **{k: item[k] for k in (
                    "category", "actual_categories", "api_category_labels", "category_status")}}
            record.update(details)
            if details.get("actual_categories") and not set(allowed).intersection(details["actual_categories"]):
                return self.mismatch(record, region, category)
        except AuthenticationError:
            raise
        except (APIError, SchemaError) as exc:
            self.failures += 1
            log.error("[Details] location_id=%s %s", identity, exc)
        if identity in self.attempted:
            return bool(record.get("reviews"))
        self.attempted.add(identity)
        saved_reviews = None if self.refresh else self.cache.get("reviews", identity)
        try:
            if saved_reviews is None:
                fetched = datetime.now(timezone.utc).isoformat()
                details = self.detail_results.get(identity, {})
                if type(details.get("review_count")) is int and details["review_count"] == 0:
                    result, metadata = [], {}
                    status = "no_site_reviews"
                else:
                    before = self.budget.estimated_used
                    result = None
                    try:
                        raw = self.client.reviews(identity, record.get("name"))
                        result = reviews(raw)
                        metadata = response_metadata(raw)
                    finally:
                        self.stats.call(region["name"], category,
                                        self.budget.estimated_used - before, result)
                    status = "complete" if result else "empty"
                self.cache.put("reviews", identity, {"reviews": result, "fetched_at": fetched,
                                                     "metadata": metadata, "review_status": status})
            else:
                result, fetched = saved_reviews["reviews"], saved_reviews["fetched_at"]
                metadata = saved_reviews.get("metadata", {"available": False})
                status = ("no_site_reviews" if not result and saved_reviews.get("review_status") == "no_site_reviews"
                          else "complete" if result else "empty")
            if status == "no_site_reviews":
                self.stats.no_site_reviews.add(identity)
            record.update(reviews=result, collected_review_count=len(result),
                          review_status=status,
                          reviews_fetched_at=fetched, review_metadata=metadata)
            log.info("[Reviews result] id=%s stored=%s total_site_reviews=%s metadata=%s",
                     identity, len(result), record.get("review_count"), metadata)
        except AuthenticationError:
            raise
        except (APIError, SchemaError) as exc:
            self.failures += 1
            record["review_status"] = "refresh_failed" if record["reviews"] else "pending"
            log.error("[Reviews] location_id=%s %s", identity, exc)
        self.processed += 1
        if self.processed % 5 == 0:
            self.save()
        return bool(record.get("reviews"))

    def run(self):
        stopped = None
        try:
            if shared(self.config):
                # Least explored regions first, then rotate after each bounded batch.
                streams = []
                for region in self.config["regions"]:
                    category = sources(self.config, self.cache, region, self.refresh)[0]
                    state = self.cache.get("searches", self.cache.search_key(self.config["city"], region, category))
                    streams.append((len((state or {}).get("items", [])), region, category))
                active = [(r, c, self.collect_search_steps(r, c)) for _, r, c in sorted(streams, key=lambda x: x[0])]
                while active:
                    following = []
                    for region, category, stream in active:
                        try:
                            next(stream)
                            following.append((region, category, stream))
                        except StopIteration:
                            pass
                        except SearchFailed as exc:
                            self.search_failure(region, category, exc)
                    active = following
            else:
                for region in self.config["regions"]:
                    for category in self.config["categories"]:
                        self.run_search(region, category)
        except BudgetExceeded as exc:
            stopped = "budget"
            log.warning("수집 중단: %s", exc)
        except KeyboardInterrupt:
            stopped = "interrupted"
            log.warning("사용자 종료: 수집한 데이터를 저장합니다.")
        except AuthenticationError as exc:
            stopped = "authentication"
            log.error("%s", exc)
        finally:
            self.save()
        return {"stopped": stopped, "failures": self.failures,
                "collection_stats": self.stats.summary(self.records),
                "failure_details": self.failure_details,
                "locations": len(self.records), "excel_saved": self.excel_ok,
                "stored_reviews": sum(len(p.get("reviews", [])) for p in self.records.values()),
                "locations_with_empty_reviews": sum(
                    p.get("review_status") == "empty" for p in self.records.values())}

    def search_failure(self, region, category, exc):
        self.failures += 1
        self.failure_details.append({"kind": "search", "region": region["name"],
                                     "category": category, "message": str(exc)})
        log.error("[Search] %s %s: %s", region["name"], category, exc)

    def run_search(self, region, category):
        try:
            self.collect_search(region, category)
        except SearchFailed as exc:
            self.search_failure(region, category, exc)

    def collect_search(self, region, category):
        for _ in self.collect_search_steps(region, category):
            pass

    def collect_search_steps(self, region, category):
        self.stats.regions.setdefault(region["name"], dict.fromkeys(self.stats.api, 0))
        self.stats.categories.setdefault(category, dict.fromkeys(self.stats.api, 0))
        key = self.cache.search_key(self.config["city"], region, category)
        state = blank_search() if self.refresh else (self.cache.get("searches", key) or blank_search())
        maximum = self.config["max_locations"]
        page_limit = math.ceil(maximum / PAGE_SIZE)
        used = set()
        reviewed = set()
        target = self.config.get("target_reviewed_locations")
        areas, progress = progress_for(state, region)
        selected = candidates(self.config, self.cache, region, category, state, self.refresh)

        def consume():
            self.stats.candidates.update((region["name"], category, p["location_id"])
                                         for p in selected[:maximum])
            batch = 0
            for item in selected[:maximum]:
                if target is not None and len(reviewed) >= target:
                    break
                if item["location_id"] not in used:
                    used.add(item["location_id"])
                    if self.collect_place(item, region, category):
                        reviewed.add(item["location_id"])
                    batch += 1
                    if batch == PAGE_SIZE:
                        batch = 0
                        yield
            if batch:
                yield

        yield from consume()
        # A single regional quota is shared across every partition, not multiplied by tiles.
        for area_index, (area, cursor) in enumerate(zip(areas, progress)):
            if len(selected) >= maximum or (target is not None and len(reviewed) >= target):
                break
            while (not cursor["exhausted"] and len(selected) < maximum
                   and (target is None or len(reviewed) < target)):
                page = cursor["next_page"]
                if page > page_limit:
                    log.warning("검색 페이지 상한 도달: 중복/변동으로 목표 수보다 적을 수 있습니다.")
                    break
                self.collect_page(area, region, category, page, state, cursor,
                                  progress, area_index, len(areas), key)
                # Keep cursor/cache tied to the original category; merge only in memory.
                seen = {p["location_id"] for p in selected}
                selected.extend(p for p in state["items"] if p["location_id"] not in seen)
                yield from consume()
                yield  # Empty/duplicate pages must also give other regions a turn.
        if not state["items"] and state["exhausted"]:
            log.warning("%s %s / %s: 검색 결과가 없습니다.",
                        self.config["city"], region["name"], category)

    def collect_page(self, area, region, category, page, state, cursor,
                     progress, area_index, area_count, key):
        log.info("[Search area] %s %s part=%s/%s page=%s", region["name"], category,
                 area_index + 1, area_count, page)
        try:
            raw = self.client.nearby(area, category, page, self.config["city"])
            items, exhausted = search_page(raw, category, page, PAGE_SIZE)
        except AuthenticationError:
            raise
        except (APIError, SchemaError) as exc:
            # Caller handles this region/category; do not advance a failed cursor.
            raise SearchFailed(region["name"], category, exc) from exc
        existing = {item["location_id"] for item in state["items"]}
        for item in items:
            if item["location_id"] not in existing:
                state["items"].append(item)
                existing.add(item["location_id"])
        cursor.update(next_page=page + 1, exhausted=exhausted)
        state.update(next_page=progress[0]["next_page"],
                     exhausted=all(p["exhausted"] for p in progress), area_progress=progress)
        self.cache.put("searches", key, state)


class SearchFailed(RuntimeError):
    pass
