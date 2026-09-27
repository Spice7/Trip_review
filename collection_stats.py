"""Per-run API yield; cached results never inflate API efficiency ratios."""


class CollectionStats:
    def __init__(self):
        self.candidates = set()
        self.mismatches = set()
        self.no_site_reviews = set()
        self.api = {"review_api_calls": 0, "locations_with_reviews": 0,
                    "empty_review_responses": 0, "reviews": 0}
        self.regions = {}
        self.categories = {}

    def call(self, region, category, count, result):
        for bucket in (self.api, self.regions.setdefault(region, dict.fromkeys(self.api, 0)),
                       self.categories.setdefault(category, dict.fromkeys(self.api, 0))):
            bucket["review_api_calls"] += count
            if result is not None:
                bucket["locations_with_reviews"] += bool(result)
                bucket["empty_review_responses"] += not result
                bucket["reviews"] += len(result)

    def summary(self, records):
        calls = self.api["review_api_calls"]
        return {
            "scope": "current_run; API yield excludes cache; groups use first calling search context",
            "candidate_locations": len(self.candidates),
            "unique_location_ids": len({x[2] for x in self.candidates}),
            "category_mismatch_locations": len(self.mismatches),
            "no_site_reviews_locations": len(self.no_site_reviews),
            **self.api,
            "final_collected_reviews": sum(len(p.get("reviews", [])) for p in records.values()),
            "locations_success_rate_percent": 100 * self.api["locations_with_reviews"] / calls if calls else 0,
            "reviews_per_review_api_call": self.api["reviews"] / calls if calls else 0,
            "by_region": self.regions, "by_category": self.categories,
        }
