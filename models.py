"""Terra v1 mappings. Only whitelisted fields survive; no photo metadata is persisted."""


class SchemaError(RuntimeError):
    pass


class CategoryMismatch(SchemaError):
    """A paid search did not obey its category filter; don't spend on its results."""


def actual_categories(raw):
    # Official Location.categories[].top_level_category enum, not the query argument.
    mapping = {"Accommodation": "HOTEL", "Attraction": "ATTRACTION",
               "Eat & Drink": "RESTAURANT"}
    rows = raw.get("categories") or []
    if not isinstance(rows, list):
        raise SchemaError("categories 필드가 배열이 아닙니다.")
    labels = list(dict.fromkeys(row.get("top_level_category") for row in rows
                               if isinstance(row, dict) and isinstance(row.get("top_level_category"), str)))
    return [mapping[label] for label in labels if label in mapping], labels


def mark_legacy_category(record):
    """Do not trust old inferred categories; keep the old value for traceability."""
    record = dict(record)
    if "category_status" not in record:
        record["legacy_category"] = record.get("category")
        record["category"] = None
        record["actual_categories"] = []
        record["category_status"] = "unverified_legacy"
    return record


def object_or_empty(value):
    return value if isinstance(value, dict) else {}


def localized(values):
    if not isinstance(values, list):
        return None, None
    rows = [v for v in values if isinstance(v, dict)]
    selected = next((v for v in rows if v.get("primary")), rows[0] if rows else {})
    return selected.get("value"), selected.get("language")


def identifier(value):
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
        raise SchemaError("Terra 응답에 올바른 숫자 ID가 없습니다.")
    return str(value)


def location(raw, category):
    if not isinstance(raw, dict):
        raise SchemaError("Location 객체 형식이 다릅니다.")
    coord = object_or_empty(raw.get("coordinates"))
    rating = object_or_empty(object_or_empty(raw.get("traveler_ratings")).get("overall"))
    addresses = raw.get("addresses") or []
    address = next((a.get("formatted") for a in addresses if isinstance(a, dict)), None)
    actual, labels = actual_categories(raw)
    return {
        "location_id": identifier(raw.get("id")), "name": localized(raw.get("names"))[0],
        "category": actual[0] if len(actual) == 1 else None,
        "actual_categories": actual, "api_category_labels": labels,
        "category_status": "verified" if actual else "unavailable",
        "address": address,
        "latitude": coord.get("latitude"), "longitude": coord.get("longitude"),
        "rating": rating.get("rating"), "review_count": rating.get("count"),
    }


def page_data(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
        raise SchemaError("Terra 응답의 data 배열이 없습니다. 완료 캐시를 만들지 않습니다.")
    return raw["data"]


def search_page(raw, category, page, size):
    entries = page_data(raw)
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SchemaError("Catalog 항목 형식이 다릅니다.")
        # Nearby result wraps the catalog projection in `location`.
        projection = entry.get("location", entry)
        item = location(projection, category)
        if item["actual_categories"] and category not in item["actual_categories"]:
            raise CategoryMismatch(
                f"검색 분류 불일치: requested={category}, id={item['location_id']}, "
                f"actual={item['actual_categories']}. 해당 결과의 후속 호출을 중단합니다.")
        result.append(item)
    pagination = object_or_empty(raw.get("pagination"))
    total_pages = pagination.get("total_pages")
    exhausted = not entries or (isinstance(total_pages, int) and page >= total_pages)
    if total_pages is None:
        exhausted = len(entries) < size
    return result, exhausted


def reviews(raw):
    result, seen = [], set()
    for entry in page_data(raw):
        if not isinstance(entry, dict):
            raise SchemaError("Review 객체 형식이 다릅니다.")
        review_id = str(entry["id"]) if entry.get("id") is not None else None
        if review_id is not None and review_id in seen:
            continue
        text, language = localized(entry.get("text"))
        title, title_language = localized(entry.get("title"))
        result.append({
            "review_id": review_id, "rating": entry.get("rating"), "title": title,
            "text": text, "trip_type": entry.get("trip_type"),
            "travel_date": entry.get("travel_date"), "published_date": entry.get("publish_ts"),
            "review_url": entry.get("url"), "language": language or title_language,
        })
        seen.add(review_id)
        if len(result) == 3:
            break
    return result


def response_metadata(raw):
    """Preserve counts/pagination/language diagnostics without reviews or photos."""
    data = page_data(raw)
    pagination = object_or_empty(raw.get("pagination"))
    language = object_or_empty(raw.get("language_meta"))
    return {
        "returned_count": len(data),
        "pagination": {k: pagination[k] for k in ("page", "size", "total_elements", "total_pages")
                       if type(pagination.get(k)) is int},
        "language_meta": {k: language[k] for k in (
            "requested_language", "total_in_pool", "matched_language", "pool_definition")
            if isinstance(language.get(k), (str, int))},
    }
