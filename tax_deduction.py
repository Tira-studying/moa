"""연말정산 신용카드 등 소득공제 추정: 올해 카드 사용액으로 공제 문턱·예상 공제액·남은 한도를 계산합니다.

계산 순서(조세특례제한법 제126조의2, 2026년 사용분 기준)
  1. 공제 대상 사용액을 종류별로 나눕니다: 신용카드 15% · 체크카드/현금영수증 30% ·
     도서·공연·영화 등 문화비 30%(총급여 7천만원 이하) · 전통시장·대중교통 40%
  2. 총급여의 25%(최저사용금액)를 공제율이 낮은 것부터 뺍니다: 신용 → 체크·문화 → 전통시장·대중교통
  3. 남은 금액에 공제율을 곱해 합하고, 기본한도(자녀 수에 따라 다름)를 넘으면 전통시장·대중교통·문화비
     공제분 안에서 추가한도까지 더 인정합니다.

한계: 모아의 거래는 가맹점 이름만 알기 때문에 대중교통·문화비는 이름으로 추정하고, 전통시장·현금영수증·
해외 결제는 구분하지 못합니다. 모아에 없는 사용액(자료가 없는 달, 현금영수증, 전통시장)은 사용자가
직접 더하는 칸(adjustments)으로 보완합니다. 실제 공제액은 홈택스 연말정산 간소화 자료로 확인해야 합니다.

호출 관계: app.py의 /api/tax-deduction* API → 이 파일의 검증·계산 함수
"""

import json
import re
import unicodedata

# 연도별 공제 규칙입니다. 세법이 바뀌면 새 연도를 추가합니다. 없는 연도는 가장 최근 규칙으로 계산하고 안내합니다.
RULES = {
    2026: {
        "threshold_rate": 0.25,
        "rates": {"credit": 0.15, "debit": 0.30, "culture": 0.30, "market": 0.40, "transit": 0.40},
        # 총급여 7천만원 이하 / 초과. 자녀 0명·1명·2명 이상 순서입니다(2026년 사용분부터 자녀 수 반영).
        "salary_line": 70_000_000,
        "basic_limit": {
            "low": [3_000_000, 3_500_000, 4_000_000],
            "high": [2_500_000, 2_750_000, 3_000_000],
        },
        "extra_limit": {"low": 3_000_000, "high": 2_000_000},
    }
}
KINDS = ("credit", "debit", "culture", "market", "transit")
KIND_NAMES = {
    "credit": "신용카드",
    "debit": "체크카드·현금영수증",
    "culture": "도서·공연·영화 등",
    "market": "전통시장",
    "transit": "대중교통",
}
# 신용카드 소득공제에서 빠지는 분야입니다(공과금·통신비·관리비, 보험료).
EXCLUDED_CATEGORIES = {"통신·공과금", "보험·금융"}
# 분야로 걸러지지 않는 제외 결제를 가맹점 이름으로 찾습니다. 이름 일부만 같아도 제외로 봅니다.
EXCLUDED_WORDS = ("관리비", "상품권", "국세", "지방세", "면세점", "하이패스", "도로공사", "통행료")
# 대중교통: 버스·지하철·철도. 택시·항공은 대중교통 공제가 아닙니다.
TRANSIT_WORDS = (
    "지하철",
    "버스",
    "코레일",
    "철도",
    "srt",
    "에스알",
    "티머니",
    "캐시비",
    "레일플러스",
    "도시철도",
    "메트로",
)
# 문화비: 도서·공연·영화·박물관·미술관·체육시설. 공제는 문화비 사업자로 등록된 곳만 인정되므로 추정입니다.
CULTURE_WORDS = (
    "교보문고",
    "영풍문고",
    "예스24",
    "알라딘",
    "서점",
    "cgv",
    "메가박스",
    "롯데시네마",
    "박물관",
    "미술관",
    "티켓",
    "공연",
)
ISSUER_TYPES = ("credit", "debit", "mixed")
MAX_AMOUNT = 10_000_000_000


def key(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or ""))).casefold()


# 1. 설정 검증과 저장
# ─────────────────────────────────────────────────────────────────
def amount(value, label, high=MAX_AMOUNT):
    if value in (None, ""):
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= high:
        raise ValueError(f"{label}은 0 이상의 정수로 입력해 주세요.")
    return value


def validate_settings(payload, issuers):
    """총급여·자녀 수·카드사별 신용/체크 구분·모아 밖 사용액 입력을 검사합니다."""
    if not isinstance(payload, dict):
        raise ValueError("설정 값을 확인해 주세요.")
    year = payload.get("year")
    if isinstance(year, bool) or not isinstance(year, int) or not 2020 <= year <= 2100:
        raise ValueError("연도를 확인해 주세요.")
    children = payload.get("children", 0)
    if isinstance(children, bool) or not isinstance(children, int) or not 0 <= children <= 20:
        raise ValueError("자녀 수는 0~20 사이로 입력해 주세요.")
    types = payload.get("issuer_types") or {}
    if not isinstance(types, dict) or any(
        name not in issuers or value not in ISSUER_TYPES for name, value in types.items()
    ):
        raise ValueError("카드사별 신용·체크 구분을 확인해 주세요.")
    adjustments = payload.get("adjustments") or {}
    if not isinstance(adjustments, dict) or any(name not in KINDS for name in adjustments):
        raise ValueError("직접 더할 사용액 종류를 확인해 주세요.")
    return {
        "year": year,
        "salary": amount(payload.get("salary"), "총급여"),
        "children": children,
        "issuer_types": types,
        "adjustments": {
            name: amount(value, KIND_NAMES[name] + " 사용액")
            for name, value in adjustments.items()
            if value
        },
    }


def load_settings(db, owner, year):
    row = db.execute(
        "SELECT * FROM tax_settings WHERE owner_id=? AND year=?", (owner, year)
    ).fetchone()
    if not row:
        return {"year": year, "salary": 0, "children": 0, "issuer_types": {}, "adjustments": {}}
    return {
        "year": year,
        "salary": row["salary"],
        "children": row["children"],
        "issuer_types": json.loads(row["issuer_types"]),
        "adjustments": json.loads(row["adjustments"]),
    }


def save_settings(db, owner, settings):
    db.execute(
        "INSERT INTO tax_settings(owner_id,year,salary,children,issuer_types,adjustments) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id,year) DO UPDATE SET salary=excluded.salary,"
        "children=excluded.children,issuer_types=excluded.issuer_types,"
        "adjustments=excluded.adjustments",
        (
            owner,
            settings["year"],
            settings["salary"],
            settings["children"],
            json.dumps(settings["issuer_types"], ensure_ascii=False),
            json.dumps(settings["adjustments"]),
        ),
    )


# 2. 거래 분류와 공제 계산
# ─────────────────────────────────────────────────────────────────
def classify(row, issuer_types, culture_allowed):
    """거래 한 건의 공제 종류를 정합니다. 공제 대상이 아니면 None입니다.

    카드사를 '혼합'(신용·체크 함께 사용)으로 두면 공제율이 낮은 신용카드로 보수적으로 계산합니다.
    """
    if row.get("payment_kind") == "bank" or row.get("excluded"):
        return None
    # 직접 입력한 현금·계좌이체는 현금영수증을 받은 것만 공제 대상(체크카드와 같은 30%)입니다.
    if row.get("payment_kind") == "manual" and not row.get("cash_receipt"):
        return None
    if row.get("category_key") in EXCLUDED_CATEGORIES:
        return None
    name = key(row.get("actual_merchant") or row["merchant"])
    if any(word in name for word in map(key, EXCLUDED_WORDS)):
        return None
    if "택시" not in name and any(word in name for word in map(key, TRANSIT_WORDS)):
        return "transit"
    if culture_allowed and any(word in name for word in map(key, CULTURE_WORDS)):
        return "culture"
    if row.get("payment_kind") == "manual" or issuer_types.get(row["card"]) == "debit":
        return "debit"
    return "credit"


def rules_for(year):
    """해당 연도 규칙과, 그 연도 규칙이 없어 다른 연도 규칙을 쓰는지 여부를 돌려줍니다."""
    if year in RULES:
        return RULES[year], False
    return RULES[max(RULES)], True


def deduction(spent, salary, children, rules):
    """종류별 사용액(spent)으로 최저사용금액 차감·공제율·한도를 적용한 예상 공제액을 계산합니다."""
    low = salary <= rules["salary_line"]
    threshold = int(salary * rules["threshold_rate"])
    # 문화비 공제율은 총급여 7천만원 이하만 해당합니다. 초과면 일반 카드 사용액으로 봅니다.
    totals = dict(spent)
    if not low:
        totals["credit"] = totals.get("credit", 0) + totals.pop("culture", 0)
    remaining = threshold
    after = {}
    # 최저사용금액은 공제율이 낮은 사용액부터 뺍니다.
    for kind in ("credit", "debit", "culture", "market", "transit"):
        used = min(remaining, max(0, totals.get(kind, 0)))
        after[kind] = max(0, totals.get(kind, 0)) - used
        remaining -= used
    portions = {kind: after[kind] * rules["rates"][kind] for kind in KINDS}
    gross = sum(portions.values())
    basic_limit = rules["basic_limit"]["low" if low else "high"][min(children, 2)]
    special = portions["market"] + portions["transit"] + (portions["culture"] if low else 0)
    extra = min(
        max(0, gross - basic_limit), special, rules["extra_limit"]["low" if low else "high"]
    )
    total_spent = sum(max(0, value) for value in totals.values())
    return {
        "threshold": threshold,
        "spent": total_spent,
        "to_threshold": max(0, threshold - total_spent),
        "gross": int(gross),
        "basic_limit": basic_limit,
        "extra": int(extra),
        "deduction": int(min(gross, basic_limit) + extra),
    }


def report(rows, settings, year, today):
    """올해 거래로 종류별 사용액, 자료가 있는 달, 연말 예상, 예상 공제액을 만듭니다."""
    rules, fallback = rules_for(year)
    low = settings["salary"] <= rules["salary_line"]
    prefix = f"{year}-"
    spent = {kind: 0 for kind in KINDS}
    months = set()
    for row in rows:
        if not row["used_on"].startswith(prefix):
            continue
        kind = classify(row, settings["issuer_types"], low)
        if kind is None:
            continue
        spent[kind] += row["amount"]
        months.add(row["used_on"][:7])
    moa_spent = dict(spent)
    for kind, value in settings["adjustments"].items():
        spent[kind] += value
    result = deduction(spent, settings["salary"], settings["children"], rules)
    # 연말 예상: 자료가 있는 달의 평균으로 올해 남은 달을 채웁니다. 이번 달은 진행 중이라 남은 달에 넣지 않습니다.
    current_month = today.month if today.year == year else (12 if today.year > year else 0)
    left = max(0, 12 - current_month)
    average = int(sum(moa_spent.values()) / len(months)) if months else 0
    projected_spent = {**spent, "credit": spent["credit"] + average * left}
    projection = deduction(projected_spent, settings["salary"], settings["children"], rules)
    return {
        "year": year,
        "rules_year": year if not fallback else max(RULES),
        "fallback_rules": fallback,
        "settings": settings,
        "salary_set": settings["salary"] > 0,
        # 종류별 사용액(모아 거래 + 직접 더한 금액). "spent"는 아래 계산 결과의 합계입니다.
        "by_kind": spent,
        "moa_spent": moa_spent,
        "months": sorted(months),
        "monthly_average": average,
        "months_left": left,
        "kind_names": KIND_NAMES,
        "rates": rules["rates"],
        **result,
        "projection": projection,
    }
