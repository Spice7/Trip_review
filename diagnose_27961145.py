"""User-authorized single V1 request. Refuse accidental repeat execution."""
from datetime import datetime, timezone

import requests

from budget import EntityBudgetManager
from config import OUTPUT_DIR, Settings
from diagnose_reviews_raw import fingerprint, run
from storage import output_lock, write_json


def main():
    settings = Settings.load()
    if not settings.api_key or settings.api_key == "your_api_key_here":
        raise ValueError("Missing API key")
    with output_lock(OUTPUT_DIR):
        marker = OUTPUT_DIR / "diagnostics/raw_27961145_v1_started.json"
        if marker.exists():
            raise ValueError("Already started; refusing repeated request")
        budget = EntityBudgetManager(OUTPUT_DIR / "entity_usage.json", settings.hard_limit)
        if budget.remaining < 1:
            raise ValueError("Insufficient budget")
        before = fingerprint(OUTPUT_DIR)
        write_json(marker, {"started_at": datetime.now(timezone.utc).isoformat()})
        with requests.Session() as session:
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            session.headers.update({"X-API-Key": settings.api_key, "Accept": "application/json"})
            run(session, budget, OUTPUT_DIR, location_id="27961145", versions=(1,))
        assert before == fingerprint(OUTPUT_DIR), "Collection files changed"
        print("Collection files unchanged. Estimated usage:", budget.estimated_used)


if __name__ == "__main__":
    main()
