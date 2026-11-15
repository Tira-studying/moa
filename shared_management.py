"""명시적으로 허용한 관리 항목만 함께 읽는 응답을 만듭니다.

원본 명세서·파일명·메일·메모·일반 거래 목록은 공개하지 않습니다. 개인 계산 결과를
그대로 반환하지 않고 필드 허용 목록으로 새 객체를 만들어 읽기 공유 범위를 고정합니다.
수정·삭제는 기존 개인 API의 소유권 검사를 그대로 사용합니다.
"""

import finance
from categories import payment_service, category_labels


def fields(record, names):
    """새로운 내부 필드가 생겨도 의도하지 않은 정보가 공유되지 않게 지정한 값만 복사합니다."""
    return {name: record[name] for name in names}


def report(db, month, cards):
    """활성 구성원의 분류·할부·중복·고정 지출·청구 설정을 읽기 전용으로 제공합니다."""
    finance.month_index(month)
    members = []
    for user in db.execute("SELECT id,username FROM users WHERE active=1 ORDER BY id").fetchall():
        private = finance.insights(db, month, owner_id=user["id"], include_bank=False)
        plans = private["installments"]
        recurring = []
        for row in private["recurring"]:
            item = fields(
                row,
                (
                    "merchant",
                    "card",
                    "amount",
                    "previous_amount",
                    "change",
                    "occurrences",
                    "last_used_on",
                    "next_expected",
                    "status",
                ),
            )
            item["history"] = [fields(payment, ("used_on", "amount")) for payment in row["history"]]
            recurring.append(item)
        labels = category_labels(db, user["id"])
        rules = [
            dict(
                merchant=row["merchant_key"], category=labels.get(row["category"], row["category"])
            )
            for row in db.execute(
                "SELECT merchant_key,category FROM merchant_categories WHERE owner_id=? ORDER BY merchant_key",
                (user["id"],),
            )
            if not payment_service(row["merchant_key"])
        ]
        private_bills = finance.billing_report(db, month, cards, owner_id=user["id"])
        billing = fields(
            private_bills, ("month", "total", "unpaid", "missing_count", "unknown_count")
        )
        billing["cards"] = [
            fields(
                row,
                (
                    "card",
                    "display_name",
                    "enabled",
                    "due_day",
                    "statement_status",
                    "amount",
                    "paid",
                    "due_on",
                    "due_override",
                ),
            )
            for row in private_bills["cards"]
        ]
        members.append(
            dict(
                username=user["username"],
                rules=rules,
                installments=dict(
                    remaining_principal=plans["remaining_principal"],
                    this_month=plans["this_month"],
                    past_unconfirmed=plans["past_unconfirmed"],
                    schedule=[fields(row, ("month", "amount")) for row in plans["schedule"]],
                    plans=[
                        fields(
                            row,
                            (
                                "name",
                                "card",
                                "principal",
                                "months",
                                "first_month",
                                "paid_count",
                                "remaining_count",
                                "remaining_principal",
                                "regular_amount",
                                "final_amount",
                                "end_month",
                                "next_month",
                            ),
                        )
                        for row in plans["plans"]
                    ],
                ),
                duplicates=[
                    dict(
                        **fields(row, ("merchant", "card", "used_on", "amount")),
                        candidate_count=len(row["items"]),
                    )
                    for row in private["duplicates"]
                ],
                excluded=[
                    fields(row, ("merchant", "card", "used_on", "amount"))
                    for row in private["excluded"]
                ],
                recurring=recurring,
                billing=billing,
            )
        )
    return dict(month=month, readonly=True, members=members)
