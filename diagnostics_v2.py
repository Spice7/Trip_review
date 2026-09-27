"""Fixed two-request review probe, isolated from the V1 collector."""

import json
from datetime import datetime, timezone

import requests

from storage import write_json
from tripadvisor_client import BASE_URL

LOCATION_IDS = ("3625822", "8671159")
PARAMS = {"version": 2, "language": "primary", "page": 1, "size": 3,
          "sort_by": "MOST_RECENT"}


def run_v2_diagnostic(key, budget, root):
    """Caller must obtain confirmation. No redirects, retries, or collection writes."""
    if budget.remaining < 2:
        raise ValueError("V2 진단에는 로컬 예산 2가 필요합니다.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = root / "diagnostics" / f"reviews_v2_{stamp}.json"
    results = []
    session = requests.Session()
    # Explicit zero retries, including connection failures and HTTP error responses.
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
    session.headers.update({"X-API-Key": key, "Accept": "application/json"})
    try:
        for location_id in LOCATION_IDS:
            row = {"location_id": location_id, "http_status": None,
                   "pagination": None, "language_meta": None, "returned_count": None}
            budget.reserve(1)
            response = None
            try:
                response = session.get(
                    f"{BASE_URL}/locations/{location_id}/reviews", params=dict(PARAMS),
                    timeout=(10, 30), allow_redirects=False,
                )
                row["http_status"] = response.status_code
                raw = response.json()
                if isinstance(raw, dict):
                    # Preserve metadata verbatim, including nested pool_definition values.
                    row["pagination"] = raw.get("pagination")
                    row["language_meta"] = raw.get("language_meta")
                    if isinstance(raw.get("data"), list):
                        row["returned_count"] = len(raw["data"])
            except (requests.RequestException, ValueError):
                # Never print exceptions, response text, or credentials. Unknown stays null.
                pass
            finally:
                if response is not None:
                    response.close()
            results.append(row)
            write_json(path, results)
            print(json.dumps(row, ensure_ascii=False, indent=2))
    finally:
        session.close()
    return path, results
