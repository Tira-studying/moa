"""분야별 월 예산: 이번 달 분야별 사용액을 예산과 비교하고, 넘었거나 빠르게 쓰는 분야를 알려 줍니다.

상태 기준
  - over: 예산을 넘었습니다.
  - warn: 예산의 80% 이상을 썼습니다.
  - pace: 80% 미만이지만 이번 달 지난 날짜 비율보다 10%p 이상 빨리 쓰고 있습니다(예: 10일에 50%).
  - ok: 그 외
사용액은 대시보드와 같이 집계에서 제외한 거래를 빼고, 계좌 지출도 포함한 순사용액(환불 차감)입니다.

호출 관계: app.py의 /api/budgets API → 이 파일의 검증·계산 함수
"""

import calendar

TOTAL = "__total__"
MAX_BUDGET = 1_000_000_000
WARN_RATIO = 0.8
PACE_MARGIN = 0.1


def validate(payload, categories):
    """[{category, amount}] 목록을 검사합니다. 금액 0이나 빈 값은 그 분야 예산을 지운다는 뜻입니다."""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) > len(categories) + 1:
        raise ValueError("예산 목록을 확인해 주세요.")
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("예산 목록을 확인해 주세요.")
        category = item.get("category")
        if category != TOTAL and category not in categories:
            raise ValueError("예산 분야를 목록에서 선택해 주세요.")
        value = item.get("amount")
        if value in (None, ""):
            value = 0
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_BUDGET:
            raise ValueError("예산은 0원 이상 10억 원 이하의 정수로 입력해 주세요.")
        if value:
            result[category] = value
    return result


def save(db, owner, budgets):
    """본인 예산을 입력한 목록으로 통째로 바꿉니다."""
    db.execute("DELETE FROM budgets WHERE owner_id=?", (owner,))
    db.executemany(
        "INSERT INTO budgets(owner_id,category,amount) VALUES(?,?,?)",
        [(owner, category, value) for category, value in budgets.items()],
    )


def load(db, owner):
    return dict(db.execute("SELECT category,amount FROM budgets WHERE owner_id=?", (owner,)))


def elapsed_ratio(month, today):
    """조회 월에서 지난 날짜 비율입니다. 지난 달은 1, 앞으로 올 달은 0입니다."""
    year, value = map(int, month.split("-"))
    current = (today.year, today.month)
    if (year, value) < current:
        return 1.0
    if (year, value) > current:
        return 0.0
    return today.day / calendar.monthrange(year, value)[1]


def status(spent, budget, elapsed):
    if spent > budget:
        return "over"
    ratio = spent / budget
    if ratio >= WARN_RATIO:
        return "warn"
    if ratio > elapsed + PACE_MARGIN:
        return "pace"
    return "ok"


def report(rows, budgets, labels, month, today):
    """예산을 정한 분야(와 전체)의 이번 달 사용액·비율·상태를 계산합니다."""
    spent = {}
    total = 0
    for row in rows:
        if row.get("excluded") or not row["used_on"].startswith(month):
            continue
        spent[row["category_key"]] = spent.get(row["category_key"], 0) + row["amount"]
        total += row["amount"]
    elapsed = elapsed_ratio(month, today)
    items = []
    for category, budget in budgets.items():
        used = total if category == TOTAL else spent.get(category, 0)
        items.append(
            {
                "category": category,
                "name": "전체" if category == TOTAL else labels.get(category, category),
                "budget": budget,
                "spent": used,
                "remaining": budget - used,
                "ratio": round(used / budget, 4),
                "status": status(used, budget, elapsed),
            }
        )
    items.sort(key=lambda item: (item["category"] != TOTAL, -item["ratio"]))
    return {
        "month": month,
        "elapsed": round(elapsed, 4),
        "items": items,
        "alerts": sum(item["status"] in ("over", "warn") for item in items),
        # 예산을 정하지 않은 분야도 이번 달 사용액을 보여 줘 처음 예산을 정할 때 참고하게 합니다.
        "spent_by_category": spent,
    }
