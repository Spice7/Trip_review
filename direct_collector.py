"""Reviews-only collection, sharing the automatic collector's cache and ledger."""
from collections import Counter
from datetime import datetime, timezone
import json

from budget import BudgetExceeded
from exporter import export
from models import SchemaError, location, response_metadata, reviews
from storage import StateError, read_json, write_json
from tripadvisor_client import APIError, AuthenticationError


def direct_plan(report, cache, budget, refresh=False):
    items = report["locations"]
    cached = sum(cache.get("reviews", p["location_id"]) is not None for p in items)
    needed = len(items) if refresh else len(items) - cached
    return {k: report[k] for k in ("input_file", "input_rows", "valid_rows", "invalid_rows")} | {
        "unique_location_ids": len(items), "already_cached": cached,
        "new_review_requests_required": needed,
        "by_region": dict(Counter(r for p in items for r in p["regions"])),
        "by_category": dict(Counter(p["category"] or "CONFLICT" for p in items)),
        "current_accumulated_usage": budget.estimated_used, "hard_limit": budget.hard_limit,
        "run_entity_budget": (budget.run_ceiling - budget.estimated_used
                              if budget.run_ceiling is not None else None),
        "maximum_api_attempts_this_run": min(needed, budget.remaining),
    }


def merge_reviews(existing, incoming):
    merged, seen = [], set()
    for review in [*existing, *incoming]:
        key = ("id", str(review["review_id"])) if review.get("review_id") is not None else (
            "content", json.dumps(review, ensure_ascii=False, sort_keys=True))
        if key not in seen:
            merged.append(review)
            seen.add(key)
    return merged


class DirectCollector:
    def __init__(self, report, cache, budget, client, root, refresh=False):
        self.report, self.cache, self.budget = report, cache, budget
        self.client, self.root, self.refresh = client, root, refresh
        saved = read_json(root / "reviews.json", [])
        if not isinstance(saved, list) or any(not isinstance(p, dict) or "location_id" not in p for p in saved):
            raise StateError("기존 reviews.json 형식을 확인하세요. 수집을 중단합니다.")
        self.records = {}
        for p in saved:
            identity = str(p["location_id"])
            if identity in self.records:
                raise StateError("기존 reviews.json에 중복 장소가 있습니다. 원본을 보존하고 확인하세요.")
            self.records[identity] = p

    def run(self):
        stats = {"mode": "manual_location_list", "input_rows": self.report["input_rows"],
                 "unique_ids": len(self.report["locations"]), "cached_locations": 0,
                 "new_review_api_calls": 0, "locations_with_reviews": 0,
                 "empty_review_responses": 0, "returned_reviews": 0,
                 "new_reviews_collected": 0, "failures": [], "stopped": None}
        try:
            for item in self.report["locations"]:
                identity = item["location_id"]
                record = self.records.setdefault(identity, {
                    **location({"id": identity}, None), "regions": [], "reviews": [],
                    "name": item["name"], "category": item["category"],
                    "category_status": "manual", "collection_source": "manual_location_list",
                    "review_status": "pending", "collected_review_count": 0,
                })
                record["regions"] = list(dict.fromkeys([*record.get("regions", []), *item["regions"]]))
                history = record.setdefault("manual_metadata", [])
                if item not in history:
                    history.append(item)
                if not record.get("tripadvisor_url"):
                    record["tripadvisor_url"] = item["tripadvisor_url"]
                if not record.get("name"):
                    record["name"] = item["name"]
                if not record.get("category") and item["category"]:
                    record.update(category=item["category"], category_status="manual")
                if item["category_conflict"]:
                    record["category_conflict"] = item["category_conflict"]
                    if record.get("category_status") == "manual":
                        record["category"] = None
                cached = self.cache.get("reviews", identity)
                if cached is not None and not self.refresh:
                    stats["cached_locations"] += 1
                    self.apply_reviews(record, cached)
                    continue
                if self.budget.remaining == 0:
                    stats["stopped"] = "budget"
                    # Still process cached entries later in the list.
                    continue
                before = self.budget.estimated_used
                try:
                    raw = self.client.reviews(identity, item["name"])
                    result = reviews(raw)
                    cached = {"reviews": result, "fetched_at": datetime.now(timezone.utc).isoformat(),
                              "review_status": "complete" if result else "empty",
                              "metadata": response_metadata(raw)}
                    self.cache.put("reviews", identity, cached)
                    stats["new_reviews_collected"] += self.apply_reviews(record, cached)
                    stats["returned_reviews"] += len(result)
                    stats["locations_with_reviews"] += bool(result)
                    stats["empty_review_responses"] += not result
                except (APIError, SchemaError) as exc:
                    status = 401 if isinstance(exc, AuthenticationError) else getattr(exc, "status", None)
                    record.update(review_status="http_error" if status else "response_error",
                                  error_status=status, review_error=str(exc))
                    stats["failures"].append({"location_id": identity, "status": status, "error": str(exc)})
                    if isinstance(exc, AuthenticationError):
                        stats["stopped"] = "authentication"
                        break
                finally:
                    stats["new_review_api_calls"] += self.budget.estimated_used - before
                if stats["new_review_api_calls"] % 5 == 0:
                    export(self.root, self.records.values())
        except BudgetExceeded:
            stats["stopped"] = "budget"
        except KeyboardInterrupt:
            stats["stopped"] = "interrupted"
        except (OSError, StateError):
            stats["stopped"] = "storage_error"
            raise
        finally:
            # Successful responses already have a durable cache; no ledger reset here.
            stats["excel_saved"] = export(self.root, self.records.values())
            calls = stats["new_review_api_calls"]
            stats["review_success_rate_percent"] = 100 * stats["locations_with_reviews"] / calls if calls else 0
            stats["reviews_per_api_call"] = stats["returned_reviews"] / calls if calls else 0
            stats["final_stored_reviews"] = sum(len(p.get("reviews", [])) for p in self.records.values())
            stats["yield_scope"] = "new API calls only; new_reviews_collected excludes existing review IDs"
            write_json(self.root / "direct_collection_stats.json", stats)
        return stats

    @staticmethod
    def apply_reviews(record, cached):
        old = merge_reviews(record.get("reviews", []), [])
        merged = merge_reviews(old, cached["reviews"])
        record.update(reviews=merged, collected_review_count=len(merged),
                      review_status="complete" if merged else cached.get("review_status", "empty"),
                      last_review_fetch_status=cached.get("review_status", "complete" if cached["reviews"] else "empty"),
                      reviews_fetched_at=cached["fetched_at"], review_metadata=cached.get("metadata", {}))
        record.pop("error_status", None)
        record.pop("review_error", None)
        return len(merged) - len(old)
