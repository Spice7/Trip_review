"""Bounded opt-in diagnostics. No mutation of review/search caches or collected outputs."""

from datetime import datetime, timezone

from budget import BudgetExceeded
from models import (SchemaError, actual_categories, identifier, object_or_empty,
                    page_data, response_metadata)
from storage import read_json, write_json
from tripadvisor_client import APIError, AuthenticationError
from spatial import search_areas


def cached_filter_conflicts(config, cache):
    """Find identical nonempty ID sets across different category searches."""
    conflicts = []
    for region in config["regions"]:
        seen = {}
        for category in config["categories"]:
            value = cache.get("searches", cache.search_key(config["city"], region, category))
            if not value:
                continue
            ids = tuple(sorted({p["location_id"] for p in value["items"]}))
            if ids and ids in seen:
                conflicts.append({"region": region["name"], "categories": [seen[ids], category],
                                  "location_ids": list(ids)})
            seen[ids] = category
    return conflicts


def diagnostic_plan(config, root):
    """Two category comparisons, largest bbox, one cached-empty review. <= 4 attempts."""
    jobs = []
    first = next((r for r in config["regions"] if r.get("search_mode") != "bbox"),
                 config["regions"][0])
    for category in ("ATTRACTION", "HOTEL"):
        jobs.append({"kind": "search", "region": search_areas(first)[0],
                     "category": category, "cost": 1})
    boxes = [r for r in config["regions"] if r.get("search_mode") == "bbox"]
    if boxes:
        largest = max(boxes, key=lambda r: (r["ne_lat"] - r["sw_lat"]) * (r["ne_lon"] - r["sw_lon"]))
        if largest != first:
            jobs.append({"kind": "search", "region": search_areas(largest)[0], "category": "ATTRACTION",
                         "cost": 1})
    records = read_json(root / "reviews.json", [])
    empty = next((p for p in records if not p.get("reviews") and (p.get("review_count") or 0) > 0), None)
    if empty:
        jobs.append({"kind": "reviews", "location_id": identifier(empty["location_id"]), "cost": 1})
    return {"jobs": jobs, "max_calls": len(jobs), "max_entities": sum(j["cost"] for j in jobs),
            "retries": 0}


def summarize(raw, kind):
    summary = response_metadata(raw)
    if kind == "search":
        items = []
        for entry in page_data(raw):
            if not isinstance(entry, dict):
                raise SchemaError("검색 응답 항목 형식 오류")
            place = object_or_empty(entry.get("location", entry))
            actual, labels = actual_categories(place)
            items.append({"location_id": identifier(place.get("id")),
                          "actual_categories": actual, "api_category_labels": labels,
                          "field_names": sorted(k for k in place if k not in (
                              "photos", "images", "user", "awards"))})
        summary["items"] = items
    else:
        # Counts/dates/IDs only; no review text, reviewer details or photo payloads.
        summary["reviews"] = [{k: row[k] for k in ("id", "publish_ts", "travel_date")
                               if isinstance(row.get(k), (str, int))}
                              for row in page_data(raw) if isinstance(row, dict)]
    return summary


def comparison_plan(config, root):
    """Compare an alternative endpoint and language defaults; no automatic fallback."""
    plan = diagnostic_plan(config, root)
    for job in plan["jobs"][:2]:
        job["source"] = "locations"
    review = next((j for j in plan["jobs"] if j["kind"] == "reviews"), None)
    if review:
        review["language"] = "primary"
        plan["jobs"].append({**review, "language": None})
    plan.update(mode="compare", max_calls=len(plan["jobs"]),
                max_entities=sum(j["cost"] for j in plan["jobs"]))
    return plan


def assess_comparison(results):
    """Describe evidence without treating HTTP 200 as proof of correct filtering."""
    findings = []
    for entry in results:
        job = entry["request"]
        if entry.get("status") != 200 or "response" not in entry:
            continue
        response = entry["response"]
        if job["kind"] == "search":
            items = response.get("items", [])
            mismatched = [p["location_id"] for p in items if p["actual_categories"]
                          and job["category"] not in p["actual_categories"]]
            unknown = [p["location_id"] for p in items if not p["actual_categories"]]
            verdict = ("empty_inconclusive" if not items else "category_mismatch" if mismatched
                       else "category_unavailable" if unknown else "returned_categories_match")
            findings.append({"region": job["region"]["name"], "category": job["category"],
                             "source": job.get("source", "catalog"), "finding": verdict,
                             "mismatched_ids": mismatched, "unclassified_ids": unknown})
        else:
            findings.append({"location_id": job["location_id"],
                             "language": job.get("language", "primary"),
                             "returned_count": response["returned_count"],
                             "finding": "reviews_returned" if response["returned_count"] else
                             "api_returned_empty_not_proof_of_no_site_reviews"})
    return findings


def run_diagnostics(plan, config, client, root):
    if client.max_attempts != 1:
        raise ValueError("진단 모드는 재시도 없이 실행해야 합니다.")
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "results": []}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = root / "diagnostics" / f"api_{stamp}.json"
    try:
        for job in plan["jobs"]:
            entry = {"request": job}
            try:
                if job["kind"] == "search":
                    raw = client.nearby(job["region"], job["category"], 1, config["city"],
                                        source=job.get("source", "catalog"))
                else:
                    raw = client.reviews(job["location_id"], language=job.get("language", "primary"))
                entry.update(status=200, response=summarize(raw, job["kind"]))
            except AuthenticationError as exc:
                entry.update(status=401, error=str(exc))
                report["results"].append(entry)
                break
            except (APIError, SchemaError) as exc:
                entry.update(status=getattr(exc, "status", None), error=str(exc))
                if isinstance(exc, APIError) and exc.status is None:
                    report["results"].append(entry)
                    report["stopped"] = "network_error"
                    break  # A blocked network must not consume the remaining local reservations.
            report["results"].append(entry)
            write_json(path, report)
    except BudgetExceeded as exc:
        report["stopped"] = str(exc)
    except KeyboardInterrupt:
        report["stopped"] = "interrupted"
    finally:
        report["findings"] = assess_comparison(report["results"])
        report["estimated_used"] = client.budget.estimated_used
        write_json(path, report)
    return path, report
