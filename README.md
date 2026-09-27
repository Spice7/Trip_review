# Tripadvisor Terra Review Collector

## Dashboard 동기화와 HTTP 시도 기준 사용량

사용자가 현재 Discover 계정에서 확인한 사례(2026-09-27):
무료 잔여량 466/1,000, 사용량 534. Requests by type은 Catalog 검색 107 + 상세 211 +
리뷰 214 + Locations 검색 2 = **534 requests = 534 free usage consumed**였습니다.
따라서 이 프로젝트의 local billing estimate는 허용된 **HTTP attempt당 1**로 계산합니다.
검색 결과가 0개/5개여도 요청 시도는 1이며, 재시도는 추가 1입니다. 실패/타임아웃도
로컬에서는 예약량을 환불하지 않으므로 Dashboard와 차이가 날 수 있습니다.
이는 현재 계정의 관찰에 맞춘 추정 모델이며 Tripadvisor 전체의 보편적인 과금 규칙을 뜻하지 않습니다.
**Dashboard가 실제 사용량/과금의 최종 기준입니다.**

이전 버전은 검색 한 번에 PAGE_SIZE=5를 예약하고 실패 시도도 포함해 로컬 사용량을
과대 추정했습니다. 기존 원장은 코드 변경으로 자동 보정하지 않습니다.
다른 수집 프로세스를 종료하고 최신 Dashboard 사용량을 확인한 후 명시적으로 동기화하세요.

```powershell
.\.venv\Scripts\python.exe main.py --sync-dashboard-usage 534
```

현재값과 새 값을 확인하고 `y`로 승인하면 `output/entity_usage.backup_<UTC시간>.json`에
기존 원본을 백업한 뒤 기준값을 바꿉니다. API를 호출하지 않습니다. 동기화 시각, 이전 값,
Dashboard 값과 누적 동기화 이력이 원장에 남으며 다음 요청에서도 유지됩니다.
다른 수집/진단 옵션과 함께 사용할 수 없고, 음수나 누적 한도를 넘는 값은 거부합니다.
승인을 거부하면 원장을 바꾸지 않습니다. 백업 또는 원장 저장이 실패해도 수집은 시작하지 않습니다.

무료 1,000 이후 유료 약 300까지 사용하려면 `.env`에 `HARD_ENTITY_BUDGET=1300`을
명시적으로 설정하세요. 기본값은 800이며 `.env.example`은 1000 예시를 제공합니다.
1,000 초과 한도에서는 유료 가능성, 무료 한도까지의 추정 잔여량, 최대 유료 추정량을
표시합니다. 별도의 중복 승인은 추가하지 않고 기존 실행 확인에서 진행 여부를 선택합니다.
로컬 제한은 실제 청구액을 보장하지 않으므로 대시보드도 함께 확인하세요.

`--run-entity-budget 50`은 이번 실행에 추가 HTTP 시도를 최대 50회 허용합니다.
534로 동기화했다면 실행 ceiling은 584이며, 누적 한도 1300과 동시에 적용됩니다.

```powershell
.\.venv\Scripts\python.exe main.py --config output/search_config.json --search-strategy shared --max-locations 130 --target-reviewed-locations 80 --run-entity-budget 50 --dry-run
# 위 계획 확인 후 --dry-run만 제거하여 실제 수집
```

### Windows 원장 저장

JSON 저장은 임시 파일 기록/fsync 후 atomic replace를 유지합니다. PermissionError 및
Windows 오류 5/32/33이면 최대 8회 교체를 시도하며, 대기는 0.05/0.1/0.2/0.4/0.8/1/1초입니다.
잠금이 풀리지 않거나 영구적인 권한 문제면 예외로 종료합니다. 원장 저장 실패 후 API를
보내지 않으며, 기존 대상 파일을 먼저 지우거나 직접 덮어쓰지 않습니다. 임시 파일은 가능한
경우 정리합니다. 지속적인 접근 거부는 파일을 점유한 프로그램이나 쓰기 권한 확인이 필요합니다.

## 실제 수집 실행

### 선택 옵션: 전체 리뷰 수 우선 조회

`--review-priority review_count`를 지정하면 후보 최대 5곳씩 상세 정보를 확인한 뒤,
해당 그룹 안에서 전체 리뷰 수가 많은 장소부터 리뷰를 요청합니다. 전체 후보를 미리 모두
조회해서 정렬하지 않습니다. 상세 요청도 HTTP 시도당 1로 예산에 포함되고 재시도도 포함됩니다.
이 옵션은 조회 순서만 바꾸며, 낮은 리뷰 수를 이유로 장소를 영구 제외하지 않습니다.
명시적 전체 리뷰 수 0은 기존처럼 생략하고, 누락/None은 그룹 뒤에서 정상 조회합니다.

캐시의 성공 장소를 먼저 목표에 포함하고, 빈 리뷰는 재조회하지 않습니다. 상세만 확인한
상태에서 목표 도달/예산 소진/사용자 중단이 발생하면 상세 캐시가 남아 다음 실행에서 재사용됩니다.
예산이 얼마 없으면 그룹을 더 작게 준비할 수 있습니다. 상세 요청의 실패/재시도 때문에
리뷰를 조회하기 전에 예산이 끝날 수 있으므로 확보율 개선을 보장하지 않습니다.
선행 상세 조회가 필요해, 작은 목표에서는 기존 순서보다 상세 조회가 더 발생할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe main.py --config output/search_config.json --search-strategy shared --max-locations 130 --target-reviewed-locations 80 --review-priority review_count --run-entity-budget 20 --dry-run
```

계획 확인 후 `--dry-run`만 제거해 소규모로 비교하세요. 실제 수집 실행 시 선택이 설정 파일에
저장됩니다. 기존 방식은 `--review-priority search_order`이며, 설정이 없을 때의 기본값입니다.
dry run은 설정 파일에 선택을 저장하지 않습니다.

기본값은 `search_strategy=shared` 효율 모드입니다. 지역마다 가장 많이 진행된 검색
카테고리의 캐시/다음 페이지를 이어가며, 다른 카테고리 캐시에만 있는 후보도 재사용합니다.
기존 검색 캐시의 요청 조건과 페이지 위치는 바꾸거나 합쳐 쓰지 않습니다.
추가 검색은 지역당 한 경로만 사용하므로 같은 장소 목록을 세 카테고리로 반복 조회하지 않습니다.
이는 분류 필터 문제의 해결이 아니라 중복 검색을 줄이는 선택입니다. 대표 검색이 모든
카테고리의 후보를 포함한다는 보장은 없습니다. 검증된 실제 분류가 선택한 카테고리 중 하나면
수집하며, 분류가 없으면 미분류 상태를 유지합니다.

덜 탐색한 지역부터 시작하고 최대 5개 후보 단위로 지역을 번갈아 처리합니다.
기본 추가 실행 예산은 50입니다. 누적 한도와 실행별 한도 중 먼저 도달하는 쪽에서 저장 후 종료합니다.
새 실행에서도 누적 사용량은 유지합니다. API의 빈 리뷰 비율 자체를 낮춘다는 보장은 없습니다.

```powershell
# 저장된 지역/카테고리 사용: 후보 최대 20곳, 리뷰 확보 장소 목표 10곳
.\.venv\Scripts\python.exe main.py --config output/search_config.json --max-locations 20 --target-reviewed-locations 10 --dry-run
.\.venv\Scripts\python.exe main.py --config output/search_config.json --max-locations 20 --target-reviewed-locations 10
```

효율 모드에서 두 한도는 **지역별 합계**입니다. 실행 확인에 `y`를 입력하면 수집합니다.
기존 카테고리별 동작은 `--search-strategy per_category`로 선택할 수 있습니다.
`--run-entity-budget 50`으로 이번 실행의 추가 로컬 예산을 명시할 수 있습니다.
일반 수집에서는 `--refresh`를 사용하지 마세요. `HARD_ENTITY_BUDGET=800`과 누적 원장을 유지합니다.
목표는 선택 사항이며 설정 JSON의 `target_reviewed_locations` 또는 CLI로 지정합니다.
한 번 지정하면 검색 설정에 저장됩니다. 목표 없이 최대 후보만 적용하려면 JSON에서 해당 키를 제거하세요.

- 일반 Reviews 요청은 V1 / primary / page=1 / size=3 / MOST_RECENT로 장소당 한 번만 시도합니다.
- HTTP 200의 빈 결과는 `empty`로 캐시하고 재조회하지 않습니다.
- 상세 정보의 전체 리뷰 수가 명시적인 정수 0이면 호출을 생략하고 `no_site_reviews`로 캐시합니다.
  누락/None은 0으로 취급하지 않습니다. 기존 정상 리뷰 캐시가 있으면 우선 재사용합니다.
- 분류가 확인되고 검색 조건과 다르면 그 검색에서 해당 장소만 `category_mismatch`로 제외합니다.
  분류가 없으면 임의로 추정하지 않고 수집합니다. 다른 일치하는 카테고리에서는 다시 후보가 될 수 있습니다.
- 캐시의 리뷰 확보 결과도 목표에 포함합니다. 목표 도달 후 남은 페이지 후보는 캐시에 남겨 다음 실행에 재사용합니다.
- V2/언어 비교 등 진단은 일반 수집에서 자동 실행하지 않습니다.

### 효율 통계

종료 출력과 `output/collection_stats.json`에 이번 실행 통계를 저장합니다.
`candidate_locations`는 지역/카테고리별 최대 후보 내에서 발견한 후보 수(캐시 포함)이며,
같은 ID가 다른 검색에 나오면 각각 셉니다. `unique_location_ids`는 이를 전역 중복 제거한 수입니다.
`category_mismatch_locations`, `no_site_reviews_locations`는 각각 해당 조건을 만난 고유 ID 수입니다.
분류 불일치 후 다른 카테고리에서 수집된 ID도 불일치 통계에 포함됩니다.

`review_api_calls`, `locations_with_reviews`, `empty_review_responses`, `reviews`는
이번 실행의 실제 요청과 응답만 셉니다. 캐시 결과는 API 효율 계산에 포함하지 않습니다.
성공률은 리뷰 확보 장소 수 / 실제 요청 수 × 100, 평균은 새로 받은 리뷰 수 / 실제 요청 수입니다.
실패 요청도 분모에 포함하고, 호출이 없으면 두 비율은 0입니다.
`final_collected_reviews`는 기존 누적 결과와 캐시를 포함한 최종 저장 리뷰 수입니다.
`by_region`, `by_category`도 이번 실행의 API 기준이며, 중복 장소는 실제 호출한 첫 검색에 귀속합니다.
따라서 지역별·카테고리별 합계가 전체 API 통계와 일치합니다.

## 일회성 V2 리뷰 진단

```powershell
.\.venv\Scripts\python.exe main.py --diagnose-v2 --dry-run
.\.venv\Scripts\python.exe main.py --diagnose-v2
```

실행 시 `y` 확인 후 `3625822`, `8671159` 각각에 한 번씩 Reviews API를 호출합니다.
이 요청에만 `version=2`, `language=primary`, `page=1`, `size=3`,
`sort_by=MOST_RECENT`를 사용합니다. 자동 재시도와 리디렉션은 없습니다.
한 요청이 HTTP 오류 또는 네트워크 오류여도 다른 ID는 한 번 시도합니다.
사용자 중단이나 저장 장치 오류가 생기면 두 요청을 모두 완료하지 못할 수 있습니다.

화면과 `output/diagnostics/reviews_v2_<UTC시간>.json`에 각 ID의
`location_id`, `http_status`, `pagination`, `language_meta`, `returned_count`만 남깁니다.
`language_meta`는 `pool_definition`의 객체/배열을 포함해 응답 값을 그대로 보존합니다.
리뷰 본문은 저장하지 않습니다. 응답/필드가 없으면 `null`이며 실제 빈 리뷰 배열은 개수 `0`입니다.
기존 캐시와 수집 JSON/Excel, 기본 `API_VERSION=1`은 변경하지 않습니다.
실행 시 누적 로컬 예산은 요청당 1씩, 총 2 증가합니다. Dry run은 호출과 예산 변경이 없습니다.
같은 명령을 다시 실행하고 승인하면 다시 두 번 호출하므로 결과 확인을 위해 재실행하지 마세요.

## 2026-09-27 수집 오류 진단 및 수정

기존 결과에서 카테고리를 요청값으로 저장한 오류를 수정했습니다. 앞으로는 API 응답의
`categories[].top_level_category`에서 실제 분류를 얻습니다. 정보가 없으면 `category=null`로
두며 요청한 카테고리로 채우지 않습니다. API 분류가 검색 조건과 다르면 해당 장소만 건너뛰고
`category_mismatch`를 기록합니다. 이전 캐시를 읽을 때는
메모리에서만 `unverified_legacy`로 표시하며 이전 값은 `legacy_category`로 보존합니다.
기존 JSON/Excel에 남은 ATTRACTION 값은 검증된 분류가 아닙니다.

서로 다른 카테고리의 캐시에 동일 장소 목록이 있으면 경고하고 수집을 계속합니다.
전역 ID 중복 제거와 캐시 재사용으로 같은 장소의 추가 호출을 방지합니다.

### 제한 진단 (실제 실행 전 별도 확인)

```powershell
# 저장된 설정만 사용, 지도와 Tripadvisor 모두 호출하지 않음
.\.venv\Scripts\python.exe main.py --diagnose --dry-run

# 계획 출력 후 y로 승인한 경우에만 실제 API 진단
.\.venv\Scripts\python.exe main.py --diagnose
```

현재 설정에서는 다음 4회, **최대 4 entities**를 예약합니다. 자동 재시도는 없습니다.

1. 광안리 관광지 검색 1회: 1
2. 같은 영역의 호텔 검색 1회: 1 (분류 필터 비교)
3. 가장 큰 사각 검색 영역인 남구의 분할 구역 1개 관광지 검색: 1
4. 전체 리뷰 수는 있으나 캐시가 빈 장소의 리뷰 조회 1회: 1

위 대상은 저장된 설정·결과에서 선택하며 실제 대상 ID와 영역은 실행 계획에 출력됩니다.
진단은 기존 검색/리뷰 캐시, JSON/Excel 결과를 덮어쓰지 않습니다. 누적 사용량 원장은
실제 요청 직전에 정상적으로 증가합니다. 결과는 `output/diagnostics/api_<UTC시간>.json`에
저장합니다. 선택한 오류 설명, 장소 ID·API 분류, 반환 수·페이지 정보·리뷰 제공 범위만 남기고
사진, 사용자 프로필, 리뷰 본문, 인증 헤더는 저장하지 않습니다.

HTTP 400은 요청 검증 오류로 안내하며 서버의 `title/detail/message`를 키 제거 후 기록합니다.
이전의 일괄적인 권한/allowlist 안내는 제거했습니다. 리뷰 응답의 `pagination` 및 제공되는
`language_meta`를 캐시에 함께 보관합니다. 빈 리뷰 응답은 `review_status=empty`로 표시하며
재실행 시 성공한 빈 응답을 자동 재요청하지 않습니다.

### 실제 진단 결과 (11:29)

남구 400의 서버 오류는 `Bounding box area must not exceed 50 square kilometers`였습니다.
이를 반영해 50㎢ 초과 범위는 49㎢ 이하의 사각형으로 분할합니다. 현재 남구는 4개로
나뉘며 전체 외곽 범위는 유지됩니다. 지역별 최대 장소 수는 4개 구역이 공유합니다.
목표 장소 수에 도달하면 추가 구역 검색을 중단합니다. 페이지/분할 구역 진행 상황도 캐시에
저장합니다. Dry run은 분할로 추가될 수 있는 검색 요청까지 포함해 보수적으로 계산합니다.

광안리 ATTRACTION/HOTEL 검색은 동일한 5개 ID·총 6개 후보를 반환했습니다.
Catalog 응답에는 실제 분류 필드가 없었습니다. 호텔 3965013의 리뷰는 HTTP 200이지만
`data=[]`, `total_elements=0`이었으며 `language_meta`는 없었습니다.
따라서 해당 빈 결과가 Excel 저장 문제는 아니라는 점은 확인됐으나, 카테고리 필터와
계정의 리뷰 제공 범위 원인은 아직 미확정입니다. 일반 수집은 분류 불일치 장소만 제외하고 계속합니다.

첫 진단은 실행 환경의 네트워크 제한으로 4회 실패했습니다. 접근 허용과 추가 예약량을
안내하고 승인받아 4회를 재실행했습니다. 당시 구형 모델의 로컬 추정량은 65→81→97이며, 첫 실패 시도도
보수적으로 포함한 값입니다. 실제 과금은 Dashboard에서 확인하세요. 후속 버전은 진단 중
네트워크 오류가 발생하면 즉시 중단해 남은 진단 요청의 로컬 예약을 방지합니다.

상세 조사 기록: [docs/diagnosis-20260927.md](docs/diagnosis-20260927.md).

Python 3.12 이상에서 실행하는 팀 프로젝트용 지역별 리뷰 수집 도구입니다.
**부산 전용** 프로그램입니다. `광안리, 수영구, 남구`처럼 세부 지역 이름만 입력하면
지도 검색으로 영역을 찾고 관광지, 호텔, 음식점의 최신 리뷰를 최대 3개씩 저장합니다.
도시 변경 기능과 위도·경도·반경 입력 단계는 없습니다.

### 지역 이름 검색 서비스

OpenStreetMap/Nominatim을 사용합니다. 추가 API Key는 필요하지 않습니다.
**[공개 서비스 이용 정책](https://operations.osmfoundation.org/policies/nominatim/)**에 따라
이 팀 도구는 한 컴퓨터에서 사람이 직접 실행하는 소규모 검색용으로 사용하세요.
전체 팀의 합산 요청은 초당 1회를 넘지 않아야 하며, 대량·주기적 수집이나 자동완성에
사용하지 마세요. 프로그램은 단일 요청 흐름, 1.1초 간격, 영구 결과 캐시를 적용합니다.
서비스 변경이 필요하면 `.env`의 `GEOCODING_URL`을 호환되는 Nominatim `/search` 주소로
지정할 수 있습니다. 출처: © OpenStreetMap contributors, ODbL.

지역 이름이 이 외부 서비스로 전송됩니다. Tripadvisor 키는 전송하지 않습니다.
지도 조회에는 Tripadvisor entity가 들지 않습니다. **Dry run도 처음 보는 지역은 지도
서비스를 조회하지만 Tripadvisor API는 호출하지 않습니다.** 저장된 검색 설정이나
위치 캐시가 있으면 지도 조회 없이 재사용합니다.

**개발 과정에서는 실제 Terra API를 호출하지 않았습니다.** 공식 문서를 기준으로
구현하고 네트워크를 차단한 가짜 응답으로 검증했습니다. 실제 키의 권한과 실제 응답은
사용자가 허용한 최초 소규모 수집에서 확인해야 합니다.

## 1. 설치

PowerShell에서 프로젝트 폴더로 이동한 다음:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

개발 시에는 `requirements-dev.txt`를 설치합니다. `requests`, `python-dotenv`,
`openpyxl`을 사용하며 pandas는 필요하지 않습니다.
이 작업 공간에는 이미 Python 3.12 가상환경과 테스트 의존성이 설치되어 있습니다.
Windows의 `python` 명령이 동작하지 않으면 `.\.venv\Scripts\python.exe`를 사용하세요.

## 2. API Key와 .env

비어 있는 `.env`를 생성해 두었습니다. **키를 입력하기 전에는 실제 수집할 수 없습니다.**
다른 팀원은 `.env.example`을 `.env`로 복사해 시작합니다.

```dotenv
TRIPADVISOR_API_KEY=본인의_실제_API_KEY
HARD_ENTITY_BUDGET=800
```

Tripadvisor Terra 개발자 Dashboard에서 Discover 계정의 키를 발급받아 입력하세요.
키는 프로젝트의 `.env`에서만 읽고 코드/명령행/로그에 넣지 않습니다.
`.env`, `output/`, `logs/`, `.venv/`는 Git에서 제외합니다.

첫 실험에서는 `HARD_ENTITY_BUDGET=100`을 권장합니다. 기본값 800은 무료 1,000 중
개발 중 수동 호출 등을 위한 200의 여유를 남깁니다. 무료 제공량은 계정 생애 동안
한 번 제공되는 수량이며 월마다 초기화되는 것으로 가정하지 않습니다.
누적 추정량과 대시보드 잔여량은 다릅니다. 누적 한도는 양의 정수로 설정하며 1,000보다 큰
값도 허용합니다. 한도 변경은 사용자가 `.env`에서 직접 해야 하며 프로그램이 자동 상향하지 않습니다.
예를 들어 누적 추정량 1,000에서 누적 한도를 1,050으로 설정하면 추가 로컬 예산은 50입니다.
기존 원장을 삭제/초기화하지 마세요. 일반 수집에는 실행별 예산 기본 50도 함께 적용됩니다.

## 3. 실행

가상환경을 활성화했다면 다음 명령을 사용할 수 있습니다.

```powershell
python main.py
python main.py --dry-run
python main.py --refresh
```

활성화하지 않았다면 `python` 대신 `.\.venv\Scripts\python.exe`를 사용하세요.
실제 수집 전에는 항상 계획을 출력하며 **`Start API collection? [y/N]`에 `y`를
입력한 경우에만** API를 호출합니다. 기본값은 취소입니다. 자동 승인 옵션은 없습니다.

### 지역 입력

세부 지역에 `광안리, 수영구, 남구`를 입력하면 각 검색 지역은
`부산 광안리`, `부산 수영구`, `부산 남구`로 구분됩니다.
다른 도시는 설정 파일에서도 거부합니다. 이전 파리 예시 설정이 남아 있으면 대화형
실행에서 사용하지 않고 부산 세부 지역을 새로 입력받습니다.

프로그램이 `부산광역시 + 입력한 이름`으로 지도 검색을 합니다. 결과의 국가·부산 소속·
장소명을 확인하며, 같은 이름의 상점이나 다른 도시의 남구는 선택하지 않습니다.
`광안리`는 `광안리해수욕장`으로 검색하는 이름 별칭을 사용하며 좌표는 하드코딩하지 않습니다.

- 수영구·남구 등 행정구역: 지도 서비스가 반환한 bounding box(사각 범위)로 검색합니다.
- 광안리 등 명소: 지도에서 반환한 중심 좌표 주변 **1km**로 검색합니다.
- 결과 없음/이름 모호함/서비스 장애: 수집을 시작하지 않고 오류와 재입력 방법을 알립니다.
  좌표를 임의로 추측하지 않습니다. 구·동을 붙인 정식 명칭을 입력하세요.

사각 범위와 원형 범위는 실제 행정 경계가 아니므로 인접 지역의 장소가 포함될 수 있습니다.
`matched_regions`는 발견된 검색 범위를 뜻합니다. 경계 밖 장소를 완전히 제외하는
다각형 필터는 제공하지 않습니다. 확정된 중심/범위/출처는 실행 계획에 표시합니다.

설정은 `output/search_config.json`에 저장됩니다. 다음 실행에서 재사용을 선택하고
장소 수만 변경할 수 있습니다. 다른 설정 파일을 지정할 수도 있습니다.

```powershell
python main.py --config output/search_config.json --max-locations 10
```

### 카테고리와 장소 수

- `1`: 관광지 ATTRACTION
- `2`: 호텔 HOTEL
- `3`: 음식점 RESTAURANT
- `4`: 전체
- `1,3`: 관광지와 음식점

최대 장소 수는 효율 모드에서 **지역별**, `per_category` 모드에서는 지역/카테고리별로 적용됩니다.
예: per_category 모드는 3지역 × 3카테고리 × 5개 = 최대 후보 45개,
shared 모드는 3지역 × 5개 = 최대 후보 15개입니다.
지역 사이의 중복은 `location_id`로 제거하므로 고유 장소 수는 더 적을 수 있습니다.

## 4. Dry run

```powershell
python main.py --dry-run
python main.py --dry-run --config examples/search_config.example.json
```

두 번째 명령은 부산 `광안리, 수영구, 남구`, 음식점, 지역마다 최대 5개를 계획합니다.
예시 JSON에는 좌표 없이 이름만 있으며 첫 실행 때 지도 조회가 필요합니다.

Dry run은 Tripadvisor 키 없이 실행할 수 있고 Tripadvisor 클라이언트를 생성하지 않습니다.
검색 설정/실행 로그/지도 위치 캐시는 저장하지만 예산 원장과 Tripadvisor 캐시는 변경하지 않습니다.
기존 캐시와 누적 사용량을 읽어 계획을 계산합니다.

캐시 없는 예시 설정의 예상 출력:

```text
DRY RUN
City: 부산
Regions: 광안리, 수영구, 남구
Categories: RESTAURANT
Max locations: 5
Maximum candidate locations: 15
Estimated search calls: 3
Estimated maximum detail calls: 15
Estimated maximum review calls: 15
Additional entities without retries: 33
Additional entities with all retries: 69
Current accumulated usage: 0
Hard limit: 800
Remaining budget: 800
Tripadvisor API 호출: 0. Entity counter 변경: 0.
```

지도 조회의 빈 결과나 모호한 결과도 반복 요청을 막기 위해 캐시합니다. 지도 데이터 변경 후
재확인이 필요하면 해당 `output/geocoding/` 지역 캐시만 별도로 보관한 뒤 제거할 수 있습니다.
`entity_usage.json`과 Tripadvisor 캐시는 삭제하지 마세요. `--refresh`는 Tripadvisor
수집 데이터만 갱신하며 지도 검색 캐시는 유지합니다.

실제 CLI는 위 정보를 JSON 필드로 출력합니다. 예산보다 예상 상한이 크면 경고하며,
실제 요청마다 예산을 검사해 가능한 부분까지만 저장합니다.

## 5. API와 Entity 계산

2026-09-27에 확인한 Terra 공식 v1 문서에 따라 `version=1`, `X-API-Key` 헤더를 사용합니다.
사용하는 endpoint는 다음 세 종류뿐입니다.

| 작업 | Endpoint | 요청 직전 예약하는 entity |
|---|---|---:|
| 좌표로 후보 검색 | `GET /api/catalog/locations/nearby` | 1 |
| 장소 상세 | `GET /api/locations/{id}` | 1 |
| 최신 리뷰 | `GET /api/locations/{id}/reviews` | 1 |

현재 계정의 Dashboard 관찰에 맞춰 모든 허용 endpoint의 HTTP 시도마다 1을 예약합니다.
반환 장소/리뷰 개수와 관계없이 예약하며, HTTP 오류나 타임아웃도 자동 환불하지 않습니다.
상세 조회는 Catalog의 간략한 데이터에 없을 수 있는 이름/주소/좌표/평점/전체 리뷰 수를
확보하기 위한 것이며, 장소별로 한 번씩 캐시합니다. Multi-GET은 사용하지 않습니다.

리뷰는 `language=primary`, `sort_by=MOST_RECENT`, `page=1`, `size=3`을 요청합니다.
Discover의 최대 3개라는 전제에 따라 반환 데이터도 최대 3개만 저장합니다.
0~2개여도 정상 완료로 캐시합니다. 리뷰 페이지를 추가로 조회하지 않습니다.

보수적 계산식:

```text
S = 새로 조회할 검색 페이지 수
D = 상세 캐시가 없는 후보 장소 수의 상한
R = 리뷰 캐시가 없는 후보 장소 수의 상한
재시도 없는 추가 entity 상한 = S + D + R
검색/상세 최대 3회, 리뷰 1회 시도 상한 = 3 × (S + D) + R
```

캐시가 없는 3지역 × 3카테고리 × 5개는 검색 9회, 상세 최대 45회, 리뷰 최대 45회이므로
per_category 모드에서 최대 **99 entities**, 검색/상세 최대 재시도 포함 **207**입니다.
이미 알려진 중복 ID는 계획에서도 한 번만 셉니다. 아직 검색하지 않은 결과의 중복은
알 수 없으므로 상한을 높게 잡습니다. 실제 과금은 Dashboard가 최종 기준입니다.

이 계산은 현재 계정의 Dashboard에서 관찰한 HTTP 요청 수와 사용량 관계를 전제로 합니다.
요금/계약이 바뀌거나 이 코드 외부에서 사용하는 같은 키의 호출량까지 자동으로
보장하지는 않습니다. 다른 팀원이 별도 폴더/PC에서 같은 키를 사용하는 경우에도
원장이 자동 합쳐지지 않습니다. **한 계정은 하나의 관리되는 원장으로 운영하세요.**

## 6. EntityBudgetManager와 안전한 재시작

`output/entity_usage.json`은 실행당 사용량이 아닌 **프로그램의 누적 추정치**입니다.

```json
{
  "hard_limit": 800,
  "estimated_used": 54,
  "remaining_local_budget": 746,
  "last_updated": "UTC ISO timestamp"
}
```

- 모든 실제 HTTP 시도 **직전** 한도를 확인하고 원장에 예약량을 영구 저장합니다.
- `현재 누적량 + 예약량 > 한도`이면 요청하지 않고 중간 결과를 저장한 뒤 정상 종료합니다.
- 원장 쓰기가 실패하면 API를 호출하지 않습니다.
- 실패/타임아웃/재시도도 로컬 사용량에 포함하고 차감 취소하지 않습니다.
- 한도를 100에서 300으로 변경해도 기존 54는 유지되어 남은 양은 246입니다.
- 한도를 기존 누적량보다 낮추면 추가 호출이 차단됩니다.
- 깨진 원장이나 캐시가 발견되면 무시하거나 0으로 초기화하지 않고 복구를 요구합니다.
- 캐시/결과는 있는데 원장이 없으면 호출을 차단합니다.
- 동일 output에 대한 두 수집기의 동시 실행은 운영체제 파일 잠금으로 차단합니다.
  강제 종료 시 잠금은 자동 해제됩니다. 남은 잠금 파일 자체는 문제가 아닙니다.

`output/`을 삭제하거나 새 작업 폴더로 옮기면 외부 계정의 사용 이력을 알 수 없습니다.
원장과 캐시를 함께 백업하고, 계정에서 이미 사용한 entity는 Dashboard와 대조하세요.
자동 월별 초기화나 사용량 초기화 명령은 제공하지 않습니다.

## 7. 캐시, Resume, Refresh

```text
output/
  geocoding/<지역명 SHA256>.json
  search_config.json
  entity_usage.json
  reviews.json
  reviews.xlsx
  cache/
    searches/<검색조건 SHA256>.json
    locations/<location_id>.json
    reviews/<location_id>.json
logs/
  collector_YYYY-MM-DD.log
```

검색 키에는 도시, 세부 지역, 좌표/반경 또는 사각 범위, 카테고리, 언어, 정렬, 페이지 크기와 스키마
버전이 포함됩니다. **최대 장소 수는 키에 포함하지 않습니다.** 5개에서 10개로 늘리면
고정 크기 5의 다음 페이지부터 검색하므로 기존 검색 페이지를 다시 호출하지 않습니다.
최대 수가 5의 배수가 아니면 마지막 페이지의 남는 후보도 검색 캐시에 보관합니다.

리뷰와 상세 정보는 지역과 무관하게 location_id별로 캐시합니다. 동일 ID가 여러
검색에 등장해도 리뷰는 한 번만 요청합니다. `--refresh` 중에도 한 실행 안에서 중복
요청하지 않습니다. 실패한 장소도 같은 실행에서 반복 방문하지 않으며 다음 실행에
다시 시도합니다. 검색/상세의 일시적 오류는 최대 2번 추가 재시도하지만 리뷰는 재시도하지 않습니다.

각 검색 페이지의 진행 위치와 성공한 상세/리뷰는 즉시 원자적으로 저장합니다.
장소 5개를 처리할 때마다 집계 JSON/Excel을 저장하고 종료 시에도 저장합니다.
오류, 예산 도달, Ctrl+C 후 재실행하면 성공 캐시는 건너뛰고 미완료 작업만 처리합니다.
HTTP 성공과 캐시 저장 사이에 프로세스가 강제 종료된 경우 서버 성공 여부를 확인할 수
없으므로 해당 요청이 다음 실행에서 다시 수행될 수 있습니다. 비용 예약은 유지됩니다.

`reviews.json`/Excel은 과거 실행 결과도 보존하는 **누적 데이터셋**입니다. 장소 수를
줄이거나 다른 부산 세부 지역을 선택해도 기존 결과를 삭제하지 않습니다. 지역은 도시를 포함한
문자열로 보존합니다. `--refresh`는 이번 설정에 해당하는 검색/장소만 새로 받습니다.
실패한 refresh는 기존 리뷰를 보존하고 `review_status=refresh_failed`로 표시합니다.

```powershell
python main.py --refresh
```

이 옵션은 추가 entity 경고와 `Continue? [y/N]` 확인을 거친 다음 일반 수집 시작 확인도
거칩니다. **둘 다 y여야** 실행하고 예산은 그대로 적용합니다.

검색 결과가 변동하거나 페이지 사이 중복이 생길 수 있으므로 검색 경로당 조회 페이지는
`ceil(최대 장소 수 / 5)`까지만 허용합니다. 이 경우 목표보다 적게 수집될 수 있음을
로그에 남기며 무제한 추가 탐색은 하지 않습니다.

## 8. JSON과 Excel

`reviews.json`은 장소 배열이며 다음과 같은 형태입니다.

```json
[
  {
    "regions": ["부산 광안리", "부산 수영구"],
    "matched_regions": ["부산 광안리", "부산 수영구"],
    "category": "RESTAURANT",
    "location_id": "123456",
    "name": "형식 설명용 장소",
    "address": null,
    "latitude": null,
    "longitude": null,
    "rating": null,
    "review_count": null,
    "collected_review_count": 1,
    "review_status": "complete",
    "reviews_fetched_at": "UTC ISO timestamp",
    "reviews": [
      {
        "review_id": "123",
        "rating": 5,
        "title": "형식 설명용 제목",
        "text": "형식 설명용 본문",
        "trip_type": null,
        "travel_date": null,
        "published_date": null,
        "language": "ko",
        "review_url": null
      }
    ]
  }
]
```

예시는 데이터 구조 설명용이며 실제 수집 결과가 아닙니다.
Terra의 `id`, `names[].value`, `addresses[].formatted`, `coordinates`,
`traveler_ratings.overall.rating/count`를 장소 필드로 변환합니다.
리뷰의 `title`/`text`는 문자열이 아닌 언어별 배열이므로 `primary` 항목을 우선 선택하며,
`publish_ts` → `published_date`, `url` → `review_url`로 매핑합니다.
없는 선택 필드는 null로 처리합니다. 구조가 잘못된 응답은 빈 성공 결과로 캐시하지 않습니다.

`reviews.xlsx`에는 다음 시트가 있습니다.

- **Locations**: regions, category, location_id, name, address, latitude, longitude,
  rating, review_count, collected_review_count
- **Reviews**: regions, category, location_id, location_name, location_rating,
  review_id, review_rating, title, text, trip_type, travel_date, published_date, language, review_url

첫 행 고정, 자동 필터, 열 너비, 줄바꿈을 적용합니다. 리뷰 문자열은 수식으로 실행되지
않도록 텍스트 셀로 저장합니다. Excel의 셀당 32,767자 제한을 넘으면 Excel만 잘라 저장하고
원문은 JSON에 유지합니다. Excel 파일이 열려 교체할 수 없으면 JSON/캐시는 유지하고
오류를 알립니다. 파일을 닫고 동일 설정으로 실행하면 캐시에서 Excel을 다시 만들 수 있습니다.

**사진 API, 이미지 다운로드, Multi-GET은 호출하지 않습니다.** 기본 상세/리뷰 응답에
API가 동반한 사진 메타데이터는 메모리에서 필드 선택 단계에서 제외하며 디스크에 쓰지
않습니다. 원본 HTTP 응답 전체를 로그나 캐시로 저장하지 않습니다.

## 9. Rate limit과 로그

검색 API 제한을 고려해 모든 HTTP 시도 사이 최소 1.1초를 둡니다.
검색/상세의 429, 5xx, 네트워크 오류는 최대 총 3회까지 지수 backoff로 재시도합니다.
일반 리뷰 요청은 오류가 나도 한 번만 시도하며 빈 응답에 대한 대체 요청도 하지 않습니다.
`Retry-After`의 초 또는 HTTP 날짜 형식을 우선 적용합니다. 60초를 넘는 대기는
더 일찍 재시도하지 않고 해당 작업을 다음 실행으로 미룹니다.
4xx는 자동 재시도하지 않고 다음 작업으로 진행합니다. 401은 잘못된 키로 불필요한
후속 요청을 보내지 않도록 전체 실행을 중단합니다.

로그에는 시작/종료, 지역, 카테고리, endpoint, ID, HTTP status, cache hit/miss,
재시도, 오류 분류와 누적 entity를 남깁니다. API Key와 응답 본문은 출력하지 않습니다.
성공/사용자 취소/dry run/예산 도달의 종료 코드는 0, 설정 오류·부분 API 실패·Excel 저장
실패 등은 2입니다. 출력의 `stopped`, `failures`도 확인하세요.

## 10. 실제 호출 전 확인할 사항

1. `.env`의 키가 Terra Discover 키인지, Dashboard에서 이미 쓴 계정 누적량이 얼마인지 확인합니다.
2. 자동 조회된 검색 영역과 카테고리, 최대 장소 수를 검토합니다. 처음에는 **지역 1개, 카테고리 1개,
   최대 5개, 한도 100**을 권장합니다.
3. Catalog 결과가 곧 조회 권한을 의미하지는 않습니다. 계정의 allowlist/라이선스에 따라
   상세와 리뷰가 403/404일 수 있습니다. Dashboard에서 필요한 권한을 확인하세요.
   프로그램은 allowlist를 자동 변경하지 않습니다.
4. **저장 용도에 대한 계약을 확인하세요.** 현재 공개 Caching Policy는 별도 계약에 명시된
   경우를 제외하면 Location ID 외 콘텐츠의 저장·복사를 허용하지 않는다고 설명합니다.
   이 프로그램이 요구하는 리뷰 캐시/JSON/Excel 보관이 팀의 계약에서 허용되는지 확인해야 합니다.
5. 먼저 dry run에서 예상 추가량과 남은 예산을 확인하고, 승인한 소규모 실행에만 y를 입력하세요.

## 11. 오프라인 검증

```powershell
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m compileall -q main.py config.py budget.py cache_manager.py collector.py exporter.py logger.py models.py storage.py tripadvisor_client.py tests
python -m pytest -q
python main.py --dry-run --config examples/search_config.example.json
```

테스트는 소켓 연결을 차단합니다. 지도 검색도 가짜 응답으로 검증합니다.
부산 소속 확인, 동명 타 도시 제외, 모호한 결과 거부, 광안리 별칭, 위치 캐시,
행정구역 bbox 요청, 좌표 질문 없는 입력 흐름, 이름 기반 dry run도 검사합니다.
예산 누적/축소/초과, 손상 원장, 동시 실행 잠금,
캐시 hit/miss, 중복 제거, 5→10 증분, refresh 확인, 실패/Ctrl+C/예산 중단 후 resume,
빈 검색, 필드 변환, 사진 제외, JSON/Excel, 수식 방지, 요청 전 예약, 429/네트워크 재시도,
검색 간격, 금지 endpoint, 키 가림, 좌표 검증, dry run의 원장 불변을 검사합니다.

## 12. 파일 구성

| 파일 | 역할 |
|---|---|
| `main.py` | 입력, CLI, dry run, 실행 확인 |
| `config.py` | .env/좌표/카테고리 검증, 상수 |
| `geocoder.py` | 부산 지역명 조회, 영역 결정, 지도 결과 캐시 |
| `tripadvisor_client.py` | 허용 endpoint, 재시도, 호출 전 예약 |
| `budget.py` | 누적 EntityBudgetManager |
| `cache_manager.py` | 검색/장소/리뷰 캐시 |
| `models.py` | 공식 v1 필드 변환, 사진 제외 |
| `collector.py` | 계획, 중복 제거, 증분 수집, resume |
| `storage.py` | 원자적 JSON 저장, 프로세스 잠금 |
| `exporter.py` | JSON/Excel 저장 |
| `logger.py` | 실행 로그와 키 가림 |
| `tests/` | 네트워크 없는 테스트 |
| `examples/search_config.example.json` | 부산 지역 이름만 포함한 dry run 예시 |
| `.env`, `.env.example`, `.gitignore` | 개인 키 설정, 공유용 예시, Git 제외 |
| `requirements.txt`, `requirements-dev.txt`, `pyproject.toml` | 의존성 및 검사 설정 |

## 공식 문서

- [Catalog Nearby: 반경, 카테고리, 페이지](https://docs.terra.tripadvisor.com/reference/cataloglocationsnearbyget)
- [Location Details: 상세 응답 구조](https://docs.terra.tripadvisor.com/reference/locationget)
- [Location Reviews: 최신 정렬과 필드](https://docs.terra.tripadvisor.com/reference/locationreviewsget)
- [리뷰 언어와 primary](https://docs.terra.tripadvisor.com/docs/supported-languages-for-reviews)
- [v1 필드 변경 내역](https://docs.terra.tripadvisor.com/changelog/version-1-in-beta)
- [Entity 과금 기준](https://docs.terra.tripadvisor.com/docs/usage-based-pricing)
- [Rate limits 및 endpoint 권한](https://docs.terra.tripadvisor.com/docs/rate-limits)
- [Caching Policy](https://docs.terra.tripadvisor.com/docs/caching-policy)
# 카테고리/빈 리뷰 비교 진단

기존 진단에서 카테고리별 동일 장소, HTTP 200의 빈 리뷰가 확인된 경우:

```powershell
.\.venv\Scripts\python.exe main.py --diagnose-compare --dry-run
.\.venv\Scripts\python.exe main.py --diagnose-compare
```

첫 명령은 API를 호출하지 않습니다. 두 번째는 `y` 확인 후 최대 5회,
현재 모델의 로컬 예산 최대 5 entities를 예약합니다. 재시도는 없습니다.
기존 검색 설정을 이용하고 리뷰/Excel/캐시는 덮어쓰지 않습니다.
`output/diagnostics/api_*.json`에 결과와 `findings`를 저장합니다.

- 첫 지역에서 `/locations/nearby`로 관광지와 호텔을 비교합니다.
- 다른 행정구역이 있으면 가장 큰 구역의 분할된 사각형 하나를 Catalog로 확인합니다.
- 저장된 빈 리뷰 장소 하나를 `language=primary`와 언어 옵션 생략으로 비교합니다.
- 적절한 지역/빈 리뷰 장소가 없으면 호출 수가 줄어듭니다.

대체 검색은 계정의 접근 허용 장소에 제한됩니다. 빈 결과는 필터 정상 작동의 증거가
아닙니다. `returned_categories_match`도 이번 응답 항목만 검증한 결과입니다.
일반 수집 경로는 자동 전환하지 않습니다. 실제 응답 검증 후 전환 여부를 판단합니다.
이 진단은 수집 완료나 과금 문제 해결을 보장하지 않습니다.

규격: https://docs.terra.tripadvisor.com/reference/locationsnearbyget
및 https://docs.terra.tripadvisor.com/reference/locationreviewsget

# 직접 Location ID 수집 모드

웹에서 최근 리뷰가 있는 장소를 먼저 선별한 뒤 Reviews API만 호출하는 방식입니다.
검색 결과의 빈 리뷰 비율이 높을 때 검색·상세 조회 비용을 줄일 수 있습니다.
웹에 리뷰가 있어도 API가 리뷰를 반환한다는 보장은 없습니다.
기존 `python main.py`, `--config`, 자동 검색 옵션은 그대로 사용할 수 있습니다.

## CSV 준비

`input/location_ids.example.csv`를 복사하고 직접 확인한 장소를 입력하세요.

```powershell
Copy-Item input/location_ids.example.csv input/location_ids.csv
```

UTF-8 CSV(BOM 허용) 형식:

```csv
region,category,location_id,name,tripadvisor_url
광안리,RESTAURANT,27961145,젤라또조이 광안리점,https://www.tripadvisor.co.kr/Restaurant_Review-g297884-d27961145-Reviews-Gelato_Joy_Gwangalli-Busan.html
```

`region`, `category`, `name`은 필수입니다. 카테고리는 `ATTRACTION`, `HOTEL`,
`RESTAURANT`이고 대소문자는 자동 정규화합니다. `location_id` 또는 `tripadvisor_url`
중 하나는 입력해야 합니다. URL만 입력하면 장소 리뷰 URL의 `-d27961145-`에서
`27961145`를 추출합니다. 둘 다 입력하면 ID가 일치해야 합니다.
이름에 쉼표가 있으면 CSV 규칙에 따라 큰따옴표로 감싸세요.

잘못된 행은 `output/location_list_errors.json`에 기록하고 제외합니다.
파일·필수 헤더 오류는 호출 전에 실행을 중단합니다. 중복 ID는 한 번만 조회하며
지역을 합칩니다. 중복 ID의 분류가 다르면 경고와 `category_conflict`를 저장합니다.
기존 API 확인 분류는 보존하고 CSV 원문 메타데이터는 `manual_metadata`에 보존합니다.
기존 분류가 없는 직접 입력 장소의 충돌 분류는 비워 둡니다.

## 실행

먼저 계획을 확인하세요. 직접 모드의 dry-run은 지도 조회와 API 호출을 모두 하지 않으며
원장·리뷰·캐시를 변경하지 않습니다. 행 검증 보고서는 저장됩니다.

```powershell
.\.venv\Scripts\python.exe main.py --location-list input/location_ids.csv --run-entity-budget 50 --dry-run
```

실제 수집은 아래 명령 실행 후 `Start API collection? [y/N]`에 `y`를 입력합니다.

```powershell
.\.venv\Scripts\python.exe main.py --location-list input/location_ids.csv --run-entity-budget 50
```

직접 모드에는 `--dry-run`, `--refresh`, `--run-entity-budget`만 함께 사용할 수 있습니다.
지역 자동 검색 옵션과는 함께 사용하지 않습니다.

## 캐시, 예산, 저장

- 기존 `output/cache/reviews/{ID}.json`을 공유합니다. 리뷰가 있는 캐시와 빈 결과
  캐시 모두 재사용하므로 같은 목록을 다시 실행하면 미완료 ID만 호출합니다.
- `--refresh`를 명시하면 추가 사용 경고와 확인 후 캐시된 ID도 다시 조회합니다.
- 각 ID는 V1, `language=primary`, `page=1`, `size=3`, `sort_by=MOST_RECENT`로
  최대 1회 호출합니다. 자동 재시도, 추가 페이지, 언어 변경, 상세 조회는 없습니다.
- 예상 호출 수는 고유 ID 수에서 캐시 ID 수를 뺀 값입니다. Refresh는 고유 ID 전체가
  대상입니다. 실제 최대 호출 수는 여기에 실행 예산(기본 50)과 누적 hard limit의
  남은 양을 함께 적용합니다. 현재 로컬 추정은 HTTP 시도당 1이며 실패도 포함합니다.
  실제 사용량과 과금은 대시보드가 기준입니다.
- 원장 저장을 완료한 뒤 요청합니다. 원장 저장 실패·인증 실패는 수집을 중단합니다.
  400/404/500 등의 개별 오류는 기록하고 다음 ID로 진행하며 빈 성공 캐시로 저장하지 않습니다.
- 성공 응답마다 캐시를 즉시 저장하고, 결과는 주기적으로 및 종료 시 기존 JSON/Excel에
  병합합니다. 기존 장소와 리뷰를 유지하고 리뷰 ID 중복을 제거합니다.
- 리뷰가 있으면 기존 상태명 `complete`, HTTP 200 빈 응답은 `empty`입니다.
  Refresh가 빈 응답을 반환해도 과거 리뷰는 유지하며 `last_review_fetch_status`에
  이번 응답 상태를 기록합니다. HTTP 오류 상태에서도 과거 리뷰는 유지합니다.
- `Locations` 시트에 출처, URL, 분류 충돌 컬럼을 추가했습니다. `Reviews` 시트는 동일합니다.
  CSV 메타데이터는 JSON의 `manual_metadata`에도 남습니다.
- 통계는 `output/direct_collection_stats.json`에 별도로 저장합니다. 성공률과 호출당
  리뷰 수는 신규 API 호출만 기준으로 합니다. `returned_reviews`는 응답 리뷰 수,
  `new_reviews_collected`는 기존 리뷰 ID를 제외한 신규 추가 수입니다.
  캐시 재사용 건수는 별도로 표시합니다.

