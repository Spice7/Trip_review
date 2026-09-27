"""Reuse one existing search stream per region instead of repeating category queries."""


def shared(config):
    return config.get("search_strategy", "per_category") == "shared"


def sources(config, cache, region, refresh=False):
    if not shared(config):
        return list(config["categories"])
    # Preserve the most advanced cache and its exact request parameters/cursor.
    def score(category):
        state = cache.get("searches", cache.search_key(config["city"], region, category))
        return len(state["items"]) if state else 0
    return [max(config["categories"], key=score)]


def candidates(config, cache, region, category, state, refresh=False):
    result = list(state["items"])
    seen = {p["location_id"] for p in result}
    if shared(config) and not refresh:
        for other in config["categories"]:
            if other == category:
                continue
            saved = cache.get("searches", cache.search_key(config["city"], region, other))
            for item in (saved or {}).get("items", []):
                if item["location_id"] not in seen:
                    result.append(item)
                    seen.add(item["location_id"])
    return result
