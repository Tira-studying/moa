"""카드 전월 실적: 카드 상품별로 이번 달 인정 실적과 다음 혜택까지 남은 금액을 계산합니다.

카드사는 이번 달 이용금액(매월 1일~말일)으로 다음 달 혜택 구간을 정합니다. 그래서 화면은
"이번 달 실적 → 다음 달 혜택"과 "지난달 실적 → 이번 달 혜택"을 함께 보여 줍니다.

거래가 어느 카드에서 나왔는지 정하는 순서 (resolve_product)
  1. 거래의 카드 번호 끝 4자리(CODEF 수집)가 등록한 카드의 끝자리와 같으면 그 카드
  2. 명세서를 통째로 특정 카드에 연결했으면 그 카드 (카드 한 장짜리 명세서)
  3. 그 카드사에 등록한 카드가 한 장뿐이면 그 카드
  4. 그 외는 "카드 미지정"으로 따로 보여 줍니다. 추측해서 아무 카드에나 넣지 않습니다.

한계: 할인받은 금액·무이자할부·상품권처럼 카드사가 실적에서 빼는 결제는 거래 자료만으로 알 수
없습니다. 분야 단위로 제외하는 것만 지원하므로 카드사 앱의 실적과 다를 수 있습니다.

호출 관계: app.py의 /api/card-performance* API → 이 파일의 검증·계산 함수
"""

import json
import re

import features

# 카드사 앱·공식 안내·상품 비교 사이트에서 확인한 처음 값입니다(2026-11 기준).
# 카드사가 조건을 자주 바꾸므로 코드에 고정하지 않고, 사용자가 "추천 값"으로 불러온 뒤 고칠 수 있게 합니다.
PRESETS = [
    {
        "issuer": "삼성",
        "name": "토스 삼성카드",
        "benefits": [
            {"label": "할인 한도 1구간", "threshold": 300000},
            {"label": "할인 한도 2구간", "threshold": 600000},
            {"label": "할인 한도 3구간", "threshold": 1000000},
        ],
        "note": "할인받은 금액, 세금·공과금, 관리비, 상품권, 무이자할부는 실적 제외",
    },
    {
        "issuer": "삼성",
        "name": "THE 1 (스카이패스)",
        "benefits": [{"label": "특별 리워드 추가 마일리지", "threshold": 300000}],
        "note": "기본 1,000원당 1마일은 실적과 무관",
    },
    {
        "issuer": "현대",
        "name": "네이버 현대카드 Edition2",
        "benefits": [{"label": "네이버페이 포인트 적립", "threshold": 500000}],
        "note": "실적 미달 시 적립 없음. 공과금·관리비·상품권·무이자할부 제외",
    },
    {
        "issuer": "현대",
        "name": "스마일카드 Edition2",
        "benefits": [],
        "note": "",
    },
    {
        "issuer": "KB국민",
        "name": "쿠팡 와우카드",
        "benefits": [],
        "note": "",
    },
    {
        "issuer": "신한",
        "name": "LG패밀리 RPM Platinum#",
        "benefits": [
            {"label": "CGV·메가박스 온라인 영화 할인", "threshold": 300000},
            {"label": "국내 전가맹점 0.8% 적립", "threshold": 500000},
        ],
        "note": "신한카드 앱 실적 안내 기준",
    },
    {
        "issuer": "우리",
        "name": "수퍼마일 체크 (스카이패스)",
        "benefits": [{"label": "마일리지 적립", "threshold": 300000}],
        "note": "",
    },
    {
        "issuer": "우리",
        "name": "위비트래블 체크",
        "benefits": [
            {"label": "캐시백 1구간", "threshold": 200000},
            {"label": "캐시백 2구간", "threshold": 500000},
            {"label": "캐시백 3구간", "threshold": 700000},
        ],
        "note": "국내 결제만 실적 인정(해외 결제는 화면에서 구분하지 못함)",
    },
    {
        "issuer": "우리",
        "name": "카드의정석2 EVERY DISCOUNT",
        "benefits": [
            {"label": "간편결제 추가 할인 1만원 한도", "threshold": 400000},
            {"label": "간편결제 추가 할인 2만원 한도", "threshold": 800000},
        ],
        "note": "기본 0.8% 할인은 실적과 무관",
    },
]

# user_features 표에서 이 기능을 가리키는 이름입니다. 기능을 끄면 탭·대시보드 요약을 숨기고 계산하지 않습니다.
FEATURE = "card_performance"
MAX_BENEFITS = 10
MAX_LAST_FOURS = 10
MAX_THRESHOLD = 100_000_000


# 1. 입력 검증
# ─────────────────────────────────────────────────────────────────
def last_four_of(card_number):
    """가려진 카드 번호(예: 5310-12**-****-1234)에서 끝 4자리 숫자만 꺼냅니다. 없으면 빈 문자열입니다."""
    match = re.search(r"(\d{4})$", re.sub(r"[-\s]", "", str(card_number or "")))
    return match.group(1) if match else ""


def validate_product(payload, issuers, categories):
    """화면에서 받은 카드 상품 입력을 검사해 저장할 값으로 바꿉니다. 잘못되면 ValueError를 냅니다."""
    if not isinstance(payload, dict):
        raise ValueError("카드 정보를 확인해 주세요.")
    issuer = payload.get("issuer")
    if issuer not in issuers:
        raise ValueError("등록된 카드사를 선택해 주세요.")
    name = str(payload.get("name") or "").strip()
    if not 1 <= len(name) <= 60 or not name.isprintable():
        raise ValueError("카드 이름은 1~60자로 입력해 주세요.")
    raw_fours = payload.get("last_fours") or []
    if isinstance(raw_fours, str):
        raw_fours = re.split(r"[,\s]+", raw_fours.strip())
    if not isinstance(raw_fours, list):
        raise ValueError("카드 끝자리를 확인해 주세요.")
    last_fours = []
    for value in raw_fours:
        value = str(value).strip()
        if not value:
            continue
        if not re.fullmatch(r"\d{4}", value):
            raise ValueError(
                "카드 끝자리는 숫자 4자리로 입력해 주세요. 여러 개는 쉼표로 구분합니다."
            )
        if value not in last_fours:
            last_fours.append(value)
    if len(last_fours) > MAX_LAST_FOURS:
        raise ValueError(f"카드 끝자리는 {MAX_LAST_FOURS}개까지 입력할 수 있습니다.")
    benefits = payload.get("benefits") or []
    if not isinstance(benefits, list) or len(benefits) > MAX_BENEFITS:
        raise ValueError(f"혜택은 {MAX_BENEFITS}개까지 입력할 수 있습니다.")
    cleaned = []
    for benefit in benefits:
        if not isinstance(benefit, dict):
            raise ValueError("혜택 정보를 확인해 주세요.")
        label = str(benefit.get("label") or "").strip()
        threshold = benefit.get("threshold")
        if not 1 <= len(label) <= 60 or not label.isprintable():
            raise ValueError("혜택 이름은 1~60자로 입력해 주세요.")
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, int)
            or not 0 < threshold <= MAX_THRESHOLD
        ):
            raise ValueError("실적 기준 금액은 1원 이상 1억 원 이하의 정수로 입력해 주세요.")
        cleaned.append({"label": label, "threshold": threshold})
    cleaned.sort(key=lambda item: item["threshold"])
    excluded = payload.get("excluded_categories") or []
    if not isinstance(excluded, list) or any(value not in categories for value in excluded):
        raise ValueError("실적 제외 분야를 목록에서 선택해 주세요.")
    note = str(payload.get("note") or "").strip()
    if len(note) > 200 or (note and not note.isprintable()):
        raise ValueError("메모는 200자 이하로 입력해 주세요.")
    return {
        "issuer": issuer,
        "name": name,
        "last_fours": last_fours,
        "benefits": cleaned,
        "excluded_categories": sorted(set(excluded), key=excluded.index),
        "note": note,
    }


def load_products(db, owner):
    """본인 카드 상품 목록을 JSON 칸을 풀어서 읽습니다."""
    products = []
    for row in db.execute(
        "SELECT * FROM card_products WHERE owner_id=? ORDER BY issuer, id", (owner,)
    ):
        product = dict(row)
        for key in ("last_fours", "benefits", "excluded_categories"):
            product[key] = json.loads(product[key] or "[]")
        products.append(product)
    return products


def is_enabled(db, owner):
    """본인이 카드 실적 기능을 켰는지 확인합니다. 화면이 복잡해지지 않도록 기본은 꺼짐입니다."""
    return features.is_enabled(db, owner, FEATURE)


def set_enabled(db, owner, enabled):
    """카드 실적 기능을 켜거나 끕니다. 꺼도 등록한 카드와 연결 설정은 지우지 않습니다."""
    features.set_enabled(db, owner, FEATURE, enabled)


# 2. 거래를 카드에 연결하고 실적 계산
# ─────────────────────────────────────────────────────────────────
def resolve_product(row, products):
    """거래 한 건이 어느 카드 상품에서 나왔는지 정합니다. 알 수 없으면 None입니다."""
    same_issuer = [p for p in products if p["issuer"] == row["card"]]
    digits = row.get("card_last_four") or ""
    if digits:
        for product in same_issuer:
            if digits in product["last_fours"]:
                return product
    chosen = row.get("import_card_product")
    if chosen:
        for product in same_issuer:
            if product["id"] == chosen:
                return product
    # 끝자리를 아는데 등록한 끝자리와 다르면, 카드가 한 장뿐이어도 끝자리를 입력하라고 보여 주는 편이
    # 안전합니다. 다만 끝자리를 하나도 입력하지 않은 한 장짜리 카드사는 그 카드로 봅니다.
    if len(same_issuer) == 1 and (not digits or not same_issuer[0]["last_fours"]):
        return same_issuer[0]
    return None


def tiers(spent, benefits):
    """혜택별 달성 여부와 남은 금액을 계산합니다."""
    return [
        {
            **benefit,
            "met": spent >= benefit["threshold"],
            "remaining": max(0, benefit["threshold"] - spent),
        }
        for benefit in benefits
    ]


def summarize(products, transactions, month, previous_month):
    """카드별 이번 달·지난달 인정 실적과 카드 미지정 거래 합계를 만듭니다.

    transactions는 app.py의 /api/data와 같은 형식(분류 적용 후)이어야 합니다.
    계좌 출금 지출과 집계에서 제외한 거래(중복 등)는 카드 실적에 넣지 않습니다.
    """
    result = {
        p["id"]: {**p, "spent": 0, "count": 0, "previous_spent": 0, "excluded_spent": 0}
        for p in products
    }
    unassigned = {}
    for row in transactions:
        if row.get("payment_kind") in ("bank", "manual") or row.get("excluded"):
            continue
        period = row["used_on"][:7]
        if period not in (month, previous_month):
            continue
        product = resolve_product(row, products)
        if product is None:
            if period == month:
                key = (row["card"], row.get("card_last_four") or "")
                bucket = unassigned.setdefault(
                    key, {"issuer": key[0], "last_four": key[1], "amount": 0, "count": 0}
                )
                bucket["amount"] += row["amount"]
                bucket["count"] += 1
            continue
        target = result[product["id"]]
        excluded = row.get("category_key") in product["excluded_categories"]
        if period == month:
            if excluded:
                target["excluded_spent"] += row["amount"]
            else:
                target["spent"] += row["amount"]
                target["count"] += 1
        elif not excluded:
            target["previous_spent"] += row["amount"]
    cards = []
    for item in result.values():
        item["tiers"] = tiers(item["spent"], item["benefits"])
        item["previous_tiers"] = tiers(item["previous_spent"], item["benefits"])
        upcoming = [tier for tier in item["tiers"] if not tier["met"]]
        item["next"] = upcoming[0] if upcoming else None
        cards.append(item)
    return {
        "month": month,
        "previous_month": previous_month,
        "cards": cards,
        "unassigned": sorted(unassigned.values(), key=lambda b: (b["issuer"], b["last_four"])),
    }
