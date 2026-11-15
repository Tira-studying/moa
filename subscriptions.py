"""구독·정기 결제 모아 보기: 반복 결제 후보(finance.recurring_candidates)를 월·연 비용과 다가오는 결제로 정리합니다.

반복 결제는 '3개월 이상 연속, 매달 한 번, 비슷한 날짜'에 결제된 가맹점입니다. 고정 지출 탭에서 '후보 제외'로
고른 결제는 빼고, 확인한 결제와 아직 검토하지 않은 후보를 함께 보여 줍니다.

  - 월 비용: 각 정기 결제의 최근 금액 합계, 연 비용은 그 12배입니다.
  - 가격 변동: 최근 결제가 직전 결제보다 비싸진 경우
  - 다가오는 결제: 기준일부터 7일 안에 결제 예정일이 있는 경우(예정일은 최근 결제일로 추정)

호출 관계: app.py의 /api/subscriptions → finance.insights의 recurring 결과 → 이 파일의 report()
"""

from datetime import date, timedelta

UPCOMING_DAYS = 7


def report(recurring, categories, labels, today):
    """반복 결제 목록에 분야 이름을 붙이고 월·연 합계, 가격 인상, 다가오는 결제를 계산합니다.

    categories: 거래 id → 분야 키 (가장 최근 결제 거래의 분야를 그 정기 결제의 분야로 봅니다)
    """
    horizon = today + timedelta(days=UPCOMING_DAYS)
    items = []
    for row in recurring:
        if row["status"] == "ignored":
            continue
        category = categories.get(row["transaction_id"], "미분류")
        expected = date.fromisoformat(row["next_expected"])
        items.append(
            {
                "merchant": row["merchant"],
                "card": row["card"],
                "amount": row["amount"],
                "change": row["change"],
                "occurrences": row["occurrences"],
                "last_used_on": row["last_used_on"],
                "next_expected": row["next_expected"],
                "status": row["status"],
                "category": labels.get(category, category),
                "subscription": category == "구독료",
                "upcoming": today <= expected <= horizon,
            }
        )
    items.sort(key=lambda item: (not item["subscription"], -item["amount"]))
    monthly = sum(item["amount"] for item in items)
    return {
        "items": items,
        "count": len(items),
        "monthly": monthly,
        "yearly": monthly * 12,
        "subscription_monthly": sum(item["amount"] for item in items if item["subscription"]),
        "increased": [item for item in items if item["change"] > 0],
        "upcoming": sorted(
            (item for item in items if item["upcoming"]), key=lambda item: item["next_expected"]
        ),
    }
