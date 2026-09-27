"""Explicit, one-off V1/V2 raw response comparison for location 8671159."""

import argparse
import hashlib
import json
from datetime import datetime, timezone

import requests

from budget import EntityBudgetManager
from config import OUTPUT_DIR, Settings
from storage import output_lock, write_json

URL = "https://terra.tripadvisor.com/api/locations/8671159/reviews"
FIELDS = ("type", "title", "status", "detail", "field_errors", "trace_id",
          "pagination", "language_meta")
HEADERS = {"date", "content-type", "content-length", "content-encoding", "server",
           "retry-after", "x-request-id", "x-correlation-id", "x-trace-id", "traceparent",
           "cf-ray", "x-amzn-trace-id", "x-amz-cf-id", "x-cache"}


def fingerprint(root):
    paths = [root / "reviews.json", root / "reviews.xlsx", *(root / "cache").rglob("*")]
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.is_file()}


def run(session, budget, root, *, location_id="8671159", versions=(1, 2)):
    if not location_id.isascii() or not location_id.isdigit() or not versions or any(
            v not in (1, 2) for v in versions):
        raise ValueError("Invalid diagnostic target")
    if budget.remaining < len(versions):
        raise ValueError("Insufficient local budget")
    url = f"https://terra.tripadvisor.com/api/locations/{location_id}/reviews"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    paths = []
    for version in versions:
        params = {"version": version, "language": "primary", "page": 1, "size": 3,
                  "sort_by": "MOST_RECENT"}
        prepared_url = requests.Request("GET", url, params=params).prepare().url
        record = {"location_id": location_id, "request_url": prepared_url,
                  "http_status": None, "response_headers": {}, "response_body": None,
                  "response_body_raw": None}
        budget.reserve(1)
        response = None
        try:
            response = session.get(url, params=params, timeout=(10, 30), allow_redirects=False)
            record["request_url"] = response.url
            record["http_status"] = response.status_code
            record["response_headers"] = {
                k: v for k, v in response.headers.items()
                if k.lower() in HEADERS or k.lower().startswith(("x-ratelimit-", "ratelimit-"))}
            # Keep the entire text as well as the complete parsed JSON, without summarizing.
            record["response_body_raw"] = response.text
            try:
                body = response.json()
            except ValueError:
                body = response.text
            record["response_body"] = body
            if isinstance(body, dict):
                record.update({k: body[k] for k in FIELDS if k in body})
                if isinstance(body.get("data"), list):
                    record["data_count"] = len(body["data"])
        except requests.RequestException as exc:
            record["transport_error"] = type(exc).__name__
        finally:
            if response is not None:
                response.close()
        path = root / "diagnostics" / f"raw_{location_id}_v{version}_{stamp}.json"
        write_json(path, record)
        paths.append(path)
        print(json.dumps({"file": str(path), "http_status": record["http_status"],
                          **{k: record[k] for k in FIELDS if k in record},
                          "data_count": record.get("data_count")}, ensure_ascii=False))
    return paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Execute the two authorized requests")
    if not parser.parse_args().execute:
        parser.error("Use --execute only after authorizing these two requests")
    settings = Settings.load()
    if not settings.api_key or settings.api_key == "your_api_key_here":
        raise ValueError("Missing API key")
    with output_lock(OUTPUT_DIR):
        marker = OUTPUT_DIR / "diagnostics/raw_8671159_started.json"
        if marker.exists():
            raise ValueError("This one-off diagnostic has already started; refusing repeated calls")
        budget = EntityBudgetManager(OUTPUT_DIR / "entity_usage.json", settings.hard_limit)
        if budget.remaining < 2:
            raise ValueError("Insufficient budget")
        before = fingerprint(OUTPUT_DIR)
        write_json(marker, {"started_at": datetime.now(timezone.utc).isoformat()})
        with requests.Session() as session:
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            session.headers.update({"X-API-Key": settings.api_key, "Accept": "application/json"})
            run(session, budget, OUTPUT_DIR)
        assert before == fingerprint(OUTPUT_DIR), "Collection files changed"
        print("Collection files unchanged. Estimated usage:", budget.estimated_used)


if __name__ == "__main__":
    main()
