"""Busan-only CLI. Terra collection requires y; name resolution uses a separate service."""

import argparse
import json
import sys
from pathlib import Path

from budget import EntityBudgetManager
from cache_manager import CacheManager
from collector import Collector, plan
from config import BASE_DIR, CATEGORIES, CITY, OUTPUT_DIR, Settings, validate_config
from geocoder import ATTRIBUTION, BusanGeocoder
from diagnostics import cached_filter_conflicts, comparison_plan, diagnostic_plan, run_diagnostics
from diagnostics_v2 import LOCATION_IDS, PARAMS, run_v2_diagnostic
from logger import setup_logging
from storage import StateError, output_lock, read_json, write_json
from tripadvisor_client import TripadvisorClient
from spatial import search_areas


def ask(prompt, convert=str, default=None):
    while True:
        value = input(f"{prompt}" + (f" [{default}]" if default is not None else "") + ": ").strip()
        if not value and default is not None:
            return default
        try:
            result = convert(value)
            if result == "":
                raise ValueError
            return result
        except (ValueError, TypeError):
            print("입력값을 확인하고 다시 입력하세요.")


def positive_max(value):
    result = int(value)
    if not 1 <= result <= 1000:
        raise ValueError
    return result


def nonnegative_int(value):
    result = int(value)
    if result < 0:
        raise ValueError("음수는 사용할 수 없습니다.")
    return result


def paid_usage_warning(budget):
    if budget.hard_limit > 1000:
        print(f"WARNING: 현재 HARD_ENTITY_BUDGET={budget.hard_limit} 입니다.")
        print("Discover 최초 무료 제공량 1,000을 초과한 사용량은 유료가 될 수 있습니다.")
        print(f"현재 local billing estimate: {budget.estimated_used}")
        print(f"무료 한도까지 남은 추정량: {max(0, 1000 - budget.estimated_used)}")
        print(f"설정된 최대 유료 사용 가능 추정량: {budget.hard_limit - 1000}")
        print("Dashboard가 최종 기준입니다. 아래 실행 확인은 유료 사용 가능성도 포함합니다.")


def categories(value):
    tokens = value.replace(" ", "").split(",")
    if "4" in tokens:
        return list(CATEGORIES)
    if any(t not in ("1", "2", "3") for t in tokens):
        raise ValueError
    return list(dict.fromkeys(CATEGORIES[int(t) - 1] for t in tokens))


def resolve_config(data, resolver):
    if not isinstance(data, dict) or data.get("city", CITY) not in (CITY, "부산광역시"):
        raise ValueError("부산 지역 설정만 사용할 수 있습니다.")
    # Validate all non-geographic inputs before any external request.
    entries = data.get("regions")
    if not isinstance(entries, list) or not entries:
        raise ValueError("세부 지역 이름이 필요합니다.")
    skeleton = [{"name": r if isinstance(r, str) else r.get("name", ""),
                 "lat": 0, "lon": 0, "radius_km": 1} for r in entries if isinstance(r, (str, dict))]
    if len(skeleton) != len(entries):
        raise ValueError("잘못된 지역 설정입니다.")
    validate_config({**data, "regions": skeleton})
    regions = []
    for entry in entries:
        if isinstance(entry, str) or "lat" not in entry:
            name = entry if isinstance(entry, str) else entry["name"]
            print(f"부산 {name}: 검색 영역 확인 중...")
            regions.append(resolver.resolve(name))
        else:
            regions.append(entry)  # Preserve previously verified settings and cache keys.
    return validate_config({**data, "regions": regions})


def interactive_config(saved, resolver):
    print("========================================\nTripadvisor Review Collector\n"
          "========================================")
    print("수집 지역: 부산 (고정)")
    if saved and saved.get("city") not in (CITY, "부산광역시"):
        print("기존 다른 도시 설정은 사용하지 않습니다. 부산 세부 지역을 입력하세요.")
        saved = None
    if saved:
        print(json.dumps(saved, ensure_ascii=False, indent=2))
        if input("저장된 검색 지역/카테고리를 재사용할까요? [y/N]: ").strip().lower() == "y":
            saved["max_locations"] = ask("최대 후보 장소 수 (효율 모드는 지역별)", positive_max,
                                         saved["max_locations"])
            return resolve_config(saved, resolver)
    names = []
    while not names:
        names = list(dict.fromkeys(x.strip() for x in ask(
            "세부 지역 (여러 지역은 쉼표로 구분)").split(",") if x.strip()))
    print("1. 관광지 (ATTRACTION)  2. 호텔 (HOTEL)  3. 음식점 (RESTAURANT)  4. 전체")
    cats = ask("카테고리 선택 (예: 1,3)", categories)
    maximum = ask("지역별 최대 후보 장소 수", positive_max, 5)
    return resolve_config({"city": CITY, "regions": names, "categories": cats,
                           "max_locations": maximum}, resolver)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Tripadvisor Terra review collector")
    parser.add_argument("--dry-run", action="store_true", help="Tripadvisor 호출 없이 계획 출력 (새 지역명은 지도 조회)")
    parser.add_argument("--refresh", action="store_true", help="경고/확인 후 캐시 새로 조회")
    parser.add_argument("--diagnose", action="store_true", help="저장된 설정으로 최대 4회 API 진단 (별도 확인 필요)")
    parser.add_argument("--diagnose-compare", action="store_true",
                        help="대체 검색/리뷰 언어 비교: 최대 5회, 로컬 추정 5 entities, 재시도 없음")
    parser.add_argument("--diagnose-v2", action="store_true",
                        help="고정된 두 장소의 V2 리뷰 진단: 각 1회, 재시도 없음")
    parser.add_argument("--config", type=Path, help="부산 지역 이름 또는 저장된 검색 설정 JSON")
    parser.add_argument("--max-locations", type=positive_max, help="설정의 장소 수만 변경")
    parser.add_argument("--target-reviewed-locations", type=positive_max,
                        help="리뷰 확보 장소 목표 (효율 모드는 지역별, 캐시 포함)")
    parser.add_argument("--search-strategy", choices=("shared", "per_category"),
                        help="shared: 지역당 검색 경로 하나 사용, per_category: 기존 카테고리별 검색")
    parser.add_argument("--run-entity-budget", type=positive_max,
                        help="이번 수집의 추가 로컬 예산 (기본 50, 누적 한도도 함께 적용)")
    parser.add_argument("--sync-dashboard-usage", type=nonnegative_int,
                        help="확인/백업 후 Dashboard 사용량으로 원장 동기화 (API 호출 없음)")
    parser.add_argument("--review-priority", choices=("search_order", "review_count"),
                        help="review_count: 신규 후보 최대 5곳의 상세 확인 후 전체 리뷰 수 순으로 조회")
    args = parser.parse_args(argv)
    if args.sync_dashboard_usage is not None and any(
            value for name, value in vars(args).items() if name != "sync_dashboard_usage"):
        parser.error("--sync-dashboard-usage는 다른 옵션과 함께 사용할 수 없습니다.")
    if args.diagnose_v2 and (args.diagnose or args.diagnose_compare or args.refresh
                            or args.config is not None or args.max_locations is not None
                            or args.target_reviewed_locations is not None or args.search_strategy is not None
                            or args.run_entity_budget is not None or args.review_priority is not None):
        parser.error("--diagnose-v2는 --dry-run 외 다른 실행 옵션과 함께 사용할 수 없습니다.")
    if args.diagnose and args.diagnose_compare:
        parser.error("--diagnose와 --diagnose-compare 중 하나만 선택하세요.")
    args.diagnose = args.diagnose or args.diagnose_compare
    if args.diagnose and (args.refresh or args.max_locations is not None
                         or args.target_reviewed_locations is not None or args.search_strategy is not None
                         or args.run_entity_budget is not None or args.review_priority is not None):
        parser.error("--diagnose는 --refresh/--max-locations와 함께 사용할 수 없습니다.")
    if sys.version_info < (3, 12):
        parser.error("Python 3.12 이상이 필요합니다.")
    try:
        settings = Settings.load()
        log = setup_logging(BASE_DIR / "logs", settings.api_key)
        log.info("실행 시작 dry_run=%s refresh=%s", args.dry_run, args.refresh)
        with output_lock(OUTPUT_DIR):
            budget = EntityBudgetManager(OUTPUT_DIR / "entity_usage.json", settings.hard_limit)
            if args.sync_dashboard_usage is not None:
                usage = args.sync_dashboard_usage
                print(f"Local estimate: {budget.estimated_used}")
                print(f"Dashboard actual usage: {usage}")
                if usage > budget.hard_limit:
                    print("WARNING: Dashboard 사용량이 hard limit보다 큽니다. 동기화를 거부합니다.")
                    return 2
                if input("Dashboard 값을 local billing baseline으로 사용할까요? [y/N]: ").strip().lower() != "y":
                    return 0
                backup = budget.sync_dashboard_usage(usage)
                print(f"원장 백업: {backup}")
                print("동기화 완료. Tripadvisor API 호출: 0회.")
                print(budget.summary())
                return 0
            paid_usage_warning(budget)
            if args.diagnose_v2:
                print(f"V2 리뷰 진단: {', '.join(LOCATION_IDS)} / 각 1회, 총 2회, 재시도 없음")
                print(json.dumps(PARAMS, ensure_ascii=False))
                print("로컬 예산 2 사용. 응답이 없거나 메타데이터가 없으면 null로 표시합니다.")
                if args.dry_run:
                    print("API 호출 0회, entity 변경 0.")
                    return 0
                if not settings.api_key or settings.api_key == "your_api_key_here":
                    print(".env의 API Key를 확인하세요.")
                    return 2
                if budget.remaining < 2:
                    print("V2 진단에 필요한 로컬 예산 2가 부족합니다.")
                    return 2
                if input("지정한 두 장소에 V2 리뷰 API를 총 2회 호출할까요? [y/N]: ").strip().lower() != "y":
                    return 0
                path, results = run_v2_diagnostic(settings.api_key, budget, OUTPUT_DIR)
                print(f"진단 저장: {path}")
                return 0 if all(r["http_status"] == 200 and r["returned_count"] is not None
                                for r in results) else 2
            cache = CacheManager(OUTPUT_DIR / "cache")
            if args.diagnose:
                # No geocoding, collection, or cache rewrites in diagnostics.
                config = validate_config(read_json(args.config or OUTPUT_DIR / "search_config.json"))
                diagnostic = (comparison_plan if args.diagnose_compare else diagnostic_plan)(config, OUTPUT_DIR)
                print("DIAGNOSTIC PLAN (기존 리뷰/캐시 보존)")
                print(json.dumps(diagnostic, ensure_ascii=False, indent=2))
                print("표시된 entities는 보수적인 로컬 예약량이며 실제 과금액이 아닙니다.")
                if args.diagnose_compare:
                    print("대체 검색은 계정 접근 범위에 제한됩니다. 빈 결과만으로 원인을 확정하지 않습니다.")
                print(budget.summary())
                if args.dry_run:
                    print("진단 계획만 출력: API 호출 0회, entity 변경 0.")
                    return 0
                if not settings.api_key or settings.api_key == "your_api_key_here":
                    print(".env의 API Key를 확인하세요.")
                    return 2
                if diagnostic["max_entities"] > budget.remaining:
                    print("진단 전체에 필요한 예산이 부족합니다. API를 호출하지 않습니다.")
                    return 2
                if input("Start diagnostic API requests? [y/N]: ").strip().lower() != "y":
                    return 0
                client = TripadvisorClient(settings.api_key, budget, max_attempts=1)
                try:
                    path, report = run_diagnostics(diagnostic, config, client, OUTPUT_DIR)
                finally:
                    client.close()
                    print(budget.summary())
                print(f"진단 저장: {path}")
                print(json.dumps(report.get("findings", []), ensure_ascii=False, indent=2))
                return 2 if report.get("stopped") or any(
                    r.get("status") != 200 or "error" in r for r in report["results"]) else 0
            print(ATTRIBUTION)
            print("처음 입력한 지역명은 지도 검색 서비스로 조회합니다. Tripadvisor entity는 사용하지 않습니다.")
            resolver = BusanGeocoder(OUTPUT_DIR / "geocoding", settings.geocoding_url)
            try:
                if args.config:
                    config = resolve_config(read_json(args.config), resolver)
                else:
                    config = interactive_config(read_json(OUTPUT_DIR / "search_config.json"), resolver)
            finally:
                resolver.close()
            if args.max_locations is not None:
                config["max_locations"] = args.max_locations
            if args.target_reviewed_locations is not None:
                config["target_reviewed_locations"] = args.target_reviewed_locations
            if args.search_strategy is not None:
                config["search_strategy"] = args.search_strategy
            if args.review_priority is not None:
                config["review_priority"] = args.review_priority
            config = validate_config(config)
            budget.limit_run(args.run_entity_budget or 50)
            if not args.dry_run:
                write_json(OUTPUT_DIR / "search_config.json", config)
            estimates = plan(config, cache, budget, args.refresh)
            conflicts = cached_filter_conflicts(config, cache)
            print("\n========================================")
            print("DRY RUN" if args.dry_run else "Collection Plan")
            print(json.dumps({**config, **estimates}, ensure_ascii=False, indent=2))
            if config["review_priority"] == "review_count":
                print("리뷰 우선순위: 신규 후보 최대 5곳씩 상세를 확인하고 전체 리뷰 수가 많은 순서로 조회합니다.")
                print("상세 조회도 예산을 사용합니다. 리뷰 수 누락은 0으로 간주하지 않고 그룹 뒤에서 조회합니다.")
            print(f"이번 실행 추가 로컬 예산: {args.run_entity_budget or 50} (누적 한도와 함께 적용)")
            if config["search_strategy"] == "shared":
                print("효율 모드: 지역당 기존 검색 경로 하나를 이어가며 지역을 번갈아 처리합니다.")
                print("후보 한도/리뷰 확보 목표는 지역별 합계입니다. 카테고리별 목표가 아닙니다.")
                print("대표 검색 조건은 실제 분류를 뜻하지 않습니다. 카테고리별 전체 탐색은 보장하지 않습니다.")
            for region in config["regions"]:
                parts = len(search_areas(region))
                if parts > 1:
                    print(f"[분할 검색] {region['name']}: {parts}개 구역. 장소 수 한도는 전체 구역이 공유합니다.")
            print("행정구역: 지도 사각 범위 / 명소: 중심 주변 1km. 실제 행정 경계와 일치하지 않을 수 있습니다.")
            print("현재 계정 관찰 기준: 검색/상세/리뷰 HTTP 시도당 로컬 추정량 1.")
            print("검색/상세는 최대 3회 시도, 리뷰는 1회만 시도합니다. 모든 시도는 남은 예산 내에서 실행됩니다.")
            if estimates["additional_entities_with_all_retries"] > budget.remaining:
                print("주의: 계획 상한이 남은 예산을 초과합니다. 예산 도달 시 중간 저장 후 종료합니다.")
            print("========================================")
            if conflicts:
                print("[검색 분류 확인 필요] 다른 카테고리의 검색 캐시에 동일 장소 목록이 있습니다.")
                print(json.dumps(conflicts, ensure_ascii=False, indent=2))
                print("중복 장소는 캐시를 재사용합니다. 확인된 분류 불일치는 해당 장소만 건너뜁니다.")
            if args.dry_run:
                print("Tripadvisor API 호출: 0. Entity counter 변경: 0.")
                print(budget.summary())
                log.info("실행 종료: dry run")
                return 0
            if not settings.api_key or settings.api_key == "your_api_key_here":
                print(".env에 TRIPADVISOR_API_KEY를 설정하세요. 실제 API는 호출하지 않았습니다.")
                return 2
            if budget.remaining == 0:
                print("누적 로컬 예산이 소진됐습니다. 원장을 유지하고 .env 한도를 명시적으로 조정해야 합니다.")
                return 2
            if args.refresh:
                print("WARNING: Refreshing cached API data will consume additional Tripadvisor entities.")
                print(f"Current estimated usage: {budget.estimated_used}; Hard limit: {budget.hard_limit}")
                if input("Continue? [y/N]: ").strip().lower() != "y":
                    return 0
            if input("Start API collection? [y/N]: ").strip().lower() != "y":
                return 0
            budget.save()
            client = TripadvisorClient(settings.api_key, budget)
            try:
                result = Collector(config, cache, budget, client, OUTPUT_DIR, args.refresh).run()
            finally:
                client.close()
                print(budget.summary())
            print(json.dumps(result, ensure_ascii=False, indent=2))
            log.info("실행 종료: %s", result)
            return 2 if result["failures"] or result["stopped"] == "authentication" or not result["excel_saved"] else 0
    except (ValueError, StateError, OSError) as exc:
        print(f"설정/저장 오류: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\n입력 취소: API 수집을 시작하지 않았습니다.")
        return 0


if __name__ == "__main__":
    # Windows redirected output otherwise defaults to a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
