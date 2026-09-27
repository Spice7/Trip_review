"""Validate the whole manual list before collection starts."""
import csv
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from config import CATEGORIES

log = logging.getLogger("collector")


def url_location_id(value):
    url = urlsplit(value)
    host = url.hostname or ""
    if url.scheme not in ("http", "https") or not re.fullmatch(
            r"(?:www\.)?tripadvisor\.(?:com|co\.[a-z]{2}|com\.[a-z]{2}|[a-z]{2})", host):
        raise ValueError("Tripadvisor URL이 아닙니다.")
    if not re.match(r"/(?:Restaurant|Hotel|Attraction)_Review-", url.path):
        raise ValueError("장소 리뷰 URL이 아닙니다.")
    match = re.search(r"-d([0-9]+)-", url.path)
    if not match:
        raise ValueError("URL에 -d숫자- Location ID가 없습니다.")
    return str(int(match[1]))


def read_location_list(path):
    entries, errors, warnings = {}, [], []
    rows = valid = 0
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            headers = reader.fieldnames or []
            if (not {"region", "category", "name"}.issubset(headers)
                    or not {"location_id", "tripadvisor_url"}.intersection(headers)
                    or len(headers) != len(set(headers))):
                raise ValueError("CSV 필수 컬럼: region, category, name 및 location_id 또는 tripadvisor_url")
            for row in reader:
                rows += 1
                try:
                    if None in row or any(v is None for v in row.values()):
                        raise ValueError("CSV 컬럼 수가 다릅니다.")
                    row = {k: v.strip() for k, v in row.items()}
                    category = row["category"].upper()
                    if not row["region"] or not row["name"] or category not in CATEGORIES:
                        raise ValueError("region/name 빈 값 또는 잘못된 category")
                    identity = row.get("location_id", "")
                    url = row.get("tripadvisor_url", "")
                    if identity and not re.fullmatch(r"[0-9]+", identity):
                        raise ValueError("location_id는 숫자여야 합니다.")
                    identity = str(int(identity)) if identity else ""
                    extracted = url_location_id(url) if url else ""
                    if identity and extracted and identity != extracted:
                        raise ValueError(f"ID mismatch: CSV={identity}, URL={extracted}")
                    identity = identity or extracted
                    if not identity or identity == "0":
                        raise ValueError("Location ID 또는 유효한 URL이 필요합니다.")
                except ValueError as exc:
                    errors.append({"row": reader.line_num, "error": str(exc), "input": row})
                    log.error("CSV row %s: %s", reader.line_num, exc)
                    continue
                valid += 1
                item = entries.setdefault(identity, {
                    "location_id": identity, "regions": [], "names": [],
                    "categories": [], "tripadvisor_urls": [],
                })
                for key, value in (("regions", row["region"]), ("names", row["name"]),
                                   ("categories", category), ("tripadvisor_urls", url)):
                    if value and value not in item[key]:
                        item[key].append(value)
    except (UnicodeError, csv.Error) as exc:
        raise ValueError(f"CSV를 읽을 수 없습니다: {exc}") from exc
    for item in entries.values():
        item["name"] = item["names"][0]
        item["tripadvisor_url"] = next(iter(item["tripadvisor_urls"]), None)
        item["category"] = item["categories"][0] if len(item["categories"]) == 1 else None
        item["category_conflict"] = item["categories"] if item["category"] is None else []
        if item["category_conflict"]:
            warning = f"{item['location_id']}: conflicting categories {item['categories']}"
            warnings.append(warning)
            log.warning(warning)
    return {"input_file": str(Path(path).resolve()), "input_rows": rows,
            "valid_rows": valid, "invalid_rows": len(errors), "errors": errors,
            "warnings": warnings, "locations": list(entries.values())}
