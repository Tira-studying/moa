"""현재 사용자의 거래로 할부·중복·고정 지출을 계산하고 개인 청구 기록을 관리합니다.

계산 결과와 실제 거래를 구분합니다. 예상 할부액이나 납부 기록이 거래 테이블에
다시 들어가 소비 합계에 중복으로 더해지지 않도록 합니다.
"""

import calendar
import re
import unicodedata
from collections import defaultdict
from datetime import date
from categories import merchant_key, payment_service
from personal_data import current_user_id


# 1. 저장 구조와 입력 검증
# ─────────────────────────────────────────────────────────────────
def schema(db):
    """할부 계획·중복 제외·반복 결제 확인·월별 청구 기록 테이블을 준비합니다."""
    db.executescript("""CREATE TABLE IF NOT EXISTS installment_plans(
        id INTEGER PRIMARY KEY,name TEXT NOT NULL,card TEXT NOT NULL,principal INTEGER NOT NULL,
        months INTEGER NOT NULL,first_month TEXT NOT NULL,paid_count INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS transaction_exclusions(
        transaction_id INTEGER PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
        created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS recurring_decisions(
        card TEXT NOT NULL,merchant_key TEXT NOT NULL,status TEXT NOT NULL,
        PRIMARY KEY(card,merchant_key));
        CREATE TABLE IF NOT EXISTS billing_settings(
        card TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 1,due_day INTEGER);
        CREATE TABLE IF NOT EXISTS monthly_bills(
        month TEXT NOT NULL,card TEXT NOT NULL,statement_status TEXT NOT NULL DEFAULT 'missing',
        amount INTEGER,due_on TEXT,paid INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(month,card));""")


def card_schema(db):
    """개인 카드 표시 이름만 별도 표에 둡니다. 기존 청구·납부·결제일 표를 다시 만들지 않습니다."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS billing_card_labels("
        "owner_id INTEGER NOT NULL REFERENCES users(id),card TEXT NOT NULL,"
        "display_name TEXT NOT NULL,PRIMARY KEY(owner_id,card))"
    )


def card_catalog(db, defaults, *, owner_id=None):
    """서버가 지정한 계정의 기본·추가 카드사와 표시 이름을 반환합니다. HTTP 소유자 인자는 받지 않습니다."""
    user_id = current_user_id() if owner_id is None else owner_id
    settings = {
        row["card"]: dict(row)
        for row in db.execute("SELECT * FROM billing_settings WHERE owner_id=?", (user_id,))
    }
    labels = {
        row["card"]: row["display_name"]
        for row in db.execute(
            "SELECT card,display_name FROM billing_card_labels WHERE owner_id=?", (user_id,)
        )
    }
    names = list(defaults) + sorted(name for name in settings if name not in defaults)
    return [
        dict(
            card=name,
            display_name=labels.get(name, name),
            enabled=bool(settings.get(name, {}).get("enabled", 1)),
            due_day=settings.get(name, {}).get("due_day"),
            custom=name not in defaults,
        )
        for name in names
    ]


def card_names(db, defaults):
    """개인 청구·할부 저장에서 등록된 본인 카드사만 허용하도록 식별자 목록을 만듭니다."""
    return [row["card"] for row in card_catalog(db, defaults)]


def validate_card_name(value):
    """URL로도 사용하는 카드사 식별자는 40자 이내의 한글·영문·숫자와 제한된 기호만 허용합니다."""
    if not isinstance(value, str):
        raise ValueError("추가할 카드사 이름을 입력해 주세요.")
    value = unicodedata.normalize("NFKC", value).strip()
    if not re.fullmatch(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9 ()·&._-]{0,39}", value):
        raise ValueError("카드사 이름은 한글·영문·숫자로 시작하는 1~40자로 입력해 주세요.")
    return value


def save_card_setting(db, card, payload):
    """본인 사용 여부·결제일·표시 이름을 함께 저장합니다. 이름 생략 요청은 기존 이름을 유지합니다."""
    if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
        raise ValueError("사용 여부를 확인해 주세요.")
    day = payload.get("due_day")
    if day is not None:
        day = integer(day, "기본 결제일", 1, 31)
    label = payload.get("display_name")
    if "display_name" in payload:
        if (
            not isinstance(label, str)
            or len(label.strip()) > 40
            or (label.strip() and not label.isprintable())
        ):
            raise ValueError("표시 이름은 줄바꿈 없는 40자 이하로 입력해 주세요.")
        label = label.strip()
    db.execute(
        "INSERT INTO billing_settings(card,enabled,due_day,owner_id) VALUES(?,?,?,?) "
        "ON CONFLICT(owner_id,card) DO UPDATE SET enabled=excluded.enabled,due_day=excluded.due_day",
        (card, int(payload["enabled"]), day, current_user_id()),
    )
    if "display_name" in payload:
        if label:
            db.execute(
                "INSERT INTO billing_card_labels(owner_id,card,display_name) VALUES(?,?,?) "
                "ON CONFLICT(owner_id,card) DO UPDATE SET display_name=excluded.display_name",
                (current_user_id(), card, label),
            )
        else:
            db.execute(
                "DELETE FROM billing_card_labels WHERE owner_id=? AND card=?",
                (current_user_id(), card),
            )


def month_index(value):
    """YYYY-MM을 비교와 월 이동에 편리한 정수로 바꾸고 입력 범위를 검사합니다."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("기준 월은 YYYY-MM 형식으로 선택해 주세요.")
    year, month = map(int, value.split("-"))
    if not 1900 <= year <= 2100 or not 1 <= month <= 12:
        raise ValueError("월을 1900~2100년 범위에서 선택해 주세요.")
    return year * 12 + month - 1


def month_name(index):
    """내부 계산용 월 정수를 다시 YYYY-MM 문자열로 바꿉니다."""
    year, month = divmod(index, 12)
    return f"{year:04}-{month+1:02}"


def integer(value, label, low, high):
    """정수로 해석 가능한 값인지, 지정한 최소·최대 범위 안인지 검사합니다."""
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"{label}을 정수로 입력해 주세요.")
    value = int(value)
    if not low <= value <= high:
        raise ValueError(f"{label} 범위를 확인해 주세요. ({low}~{high})")
    return value


def validate_plan(payload, cards):
    """할부 이름·원금·회차·첫 납부 월을 검사해 저장 가능한 값으로 정리합니다."""
    if not isinstance(payload, dict):
        raise ValueError("할부 내용을 입력해 주세요.")
    name = payload.get("name")
    card = payload.get("card")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("할부 이름은 1~160자로 입력해 주세요.")
    if card not in cards:
        raise ValueError("카드사를 선택해 주세요.")
    months = integer(payload.get("months"), "총 회차", 2, 60)
    principal = integer(payload.get("principal"), "할부 원금", months, 10**12)
    paid = integer(payload.get("paid_count", 0), "납부 완료 회차", 0, months)
    month_index(payload.get("first_month"))
    return dict(
        name=name.strip(),
        card=card,
        principal=principal,
        months=months,
        first_month=payload["first_month"],
        paid_count=paid,
    )


# 2. 할부 원금·월별 예상액 계산
# ─────────────────────────────────────────────────────────────────
def installment_report(plans, base):
    """직접 입력한 할부 원금을 균등 분할해 남은 원금과 12개월 납부 예상액을 계산합니다."""
    start = month_index(base)
    schedule = {month_name(start + i): 0 for i in range(12)}
    result = []
    past = 0
    for raw in plans:
        plan = dict(raw)
        first = month_index(plan["first_month"])
        count = plan["months"]
        paid = plan["paid_count"]
        regular = plan["principal"] // count
        payments = [regular] * (count - 1) + [plan["principal"] - regular * (count - 1)]
        remaining = payments[paid:]
        next_index = first + paid
        plan.update(
            remaining_count=count - paid,
            remaining_principal=sum(remaining),
            regular_amount=regular,
            final_amount=payments[-1],
            end_month=month_name(first + count - 1),
            next_month=month_name(next_index) if remaining else None,
        )
        for i in range(paid, count):
            key = month_name(first + i)
            if first + i < start:
                past += payments[i]
            if key in schedule:
                schedule[key] += payments[i]
        result.append(plan)
    return dict(
        plans=result,
        schedule=[dict(month=key, amount=value) for key, value in schedule.items()],
        remaining_principal=sum(p["remaining_principal"] for p in result),
        past_unconfirmed=past,
        this_month=schedule[base],
    )


# 3. 서로 다른 파일의 중복 후보 확인
# ─────────────────────────────────────────────────────────────────
def duplicate_groups(rows):
    """서로 다른 파일에서 카드사·이용일·가맹점·부호가 있는 금액이 같은 거래를 묶습니다."""
    grouped = defaultdict(list)
    for row in rows:
        if not row.get("excluded"):
            grouped[
                (row["card"], row["used_on"], merchant_key(row["merchant"]), row["amount"])
            ].append(row)
    return [
        dict(
            card=key[0], used_on=key[1], merchant=values[0]["merchant"], amount=key[3], items=values
        )
        for key, values in grouped.items()
        if len({row["import_id"] for row in values}) > 1
    ]


def new_duplicate_count(db, import_id):
    """방금 저장한 파일과 다른 파일 사이에 겹치는 거래 후보 수를 셉니다."""
    rows = [
        dict(row)
        for row in db.execute(
            """SELECT t.*,i.card,CASE WHEN e.transaction_id IS NULL THEN 0 ELSE 1 END AS excluded
        FROM transactions t JOIN imports i ON i.id=t.import_id
        LEFT JOIN transaction_exclusions e ON e.transaction_id=t.id WHERE i.owner_id=?""",
            (current_user_id(),),
        )
    ]
    return sum(
        row["import_id"] == import_id for group in duplicate_groups(rows) for row in group["items"]
    )


def can_exclude(db, row):
    """다른 파일에 아직 집계 중인 같은 거래가 남아 있는지 확인합니다."""
    key = merchant_key(row["merchant"])
    others = db.execute(
        """SELECT t.merchant,t.import_id FROM transactions t JOIN imports i ON i.id=t.import_id
        LEFT JOIN transaction_exclusions e ON e.transaction_id=t.id
        WHERE i.owner_id=? AND i.card=? AND t.used_on=? AND t.amount=? AND t.id!=? AND e.transaction_id IS NULL""",
        (current_user_id(), row["card"], row["used_on"], row["amount"], row["id"]),
    )
    return any(
        other["import_id"] != row["import_id"] and merchant_key(other["merchant"]) == key
        for other in others
    )


# 4. 연속된 결제에서 고정 지출 후보 찾기
# ─────────────────────────────────────────────────────────────────
def recurring_candidates(rows, base, decisions):
    """연속 월에 한 번씩 비슷한 날짜에 발생한 결제를 반복 지출 후보로 찾습니다."""
    end = month_index(base)
    grouped = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row.get("excluded"):
            continue
        name = row.get("actual_merchant") or row["merchant"]
        if payment_service(name):
            continue
        try:
            month = month_index(row["used_on"][:7])
        except ValueError:
            continue
        if month > end:
            continue
        grouped[(row["card"], merchant_key(name))][month].append(row)
    result = []
    for key, months in grouped.items():
        latest = max(months)
        if latest < end - 1:
            continue
        sequence = []
        cursor = latest
        while cursor in months and len(sequence) < 12:
            values = months[cursor]
            positive = [row for row in values if row["amount"] > 0]
            if len(positive) != 1 or any(row["amount"] < 0 for row in values):
                break
            sequence.append(positive[0])
            cursor -= 1
        if len(sequence) < 3:
            continue
        days = [int(row["used_on"][-2:]) for row in sequence]
        if max(days) - min(days) > 7:
            continue
        current, previous = sequence[:2]
        next_month = month_name(latest + 1)
        year, month = map(int, next_month.split("-"))
        next_day = min(int(current["used_on"][-2:]), calendar.monthrange(year, month)[1])
        result.append(
            dict(
                card=key[0],
                merchant_key=key[1],
                merchant=current.get("actual_merchant") or current["merchant"],
                transaction_id=current["id"],
                amount=current["amount"],
                previous_amount=previous["amount"],
                change=current["amount"] - previous["amount"],
                occurrences=len(sequence),
                last_used_on=current["used_on"],
                next_expected=f"{next_month}-{next_day:02}",
                status=decisions.get(key, "candidate"),
                history=list(reversed(sequence)),
            )
        )
    return sorted(result, key=lambda row: (row["status"] == "ignored", -row["amount"]))


# 5. 화면에 보낼 분석 묶음
# ─────────────────────────────────────────────────────────────────
def insights(db, base, *, owner_id=None, include_bank=True):
    """DB에서 필요한 거래를 읽고 할부·중복·고정 지출 결과를 하나의 응답으로 묶습니다."""
    month_index(base)
    # 개인 API는 기본값을 사용하고, 공유 읽기 계층만 서버가 확인한 계정을 지정합니다.
    user_id = current_user_id() if owner_id is None else owner_id
    rows = [
        dict(row)
        for row in db.execute(
            """SELECT t.*,i.card,i.filename,i.created AS imported_at,COALESCE(a.actual_merchant,'') AS actual_merchant,
        CASE WHEN i.digest LIKE 'bank-expense:%' THEN 'bank' WHEN i.digest LIKE 'manual:%' THEN 'manual'
        ELSE 'card' END AS payment_kind,
        CASE WHEN e.transaction_id IS NULL THEN 0 ELSE 1 END AS excluded
        FROM transactions t JOIN imports i ON i.id=t.import_id
        LEFT JOIN transaction_exclusions e ON e.transaction_id=t.id LEFT JOIN transaction_annotations a ON a.transaction_id=t.id WHERE i.owner_id=? ORDER BY t.used_on DESC,t.id DESC""",
            (user_id,),
        )
    ]
    card_rows = [row for row in rows if row["payment_kind"] == "card"]
    if not include_bank:
        rows = card_rows
    decisions = {
        (row["card"], row["merchant_key"]): row["status"]
        for row in db.execute("SELECT * FROM recurring_decisions WHERE owner_id=?", (user_id,))
    }
    return dict(
        installments=installment_report(
            db.execute(
                "SELECT * FROM installment_plans WHERE owner_id=? ORDER BY first_month,id",
                (user_id,),
            ).fetchall(),
            base,
        ),
        duplicates=duplicate_groups(card_rows),
        excluded=[row for row in card_rows if row["excluded"]],
        recurring=recurring_candidates(rows, base, decisions),
    )


# 6. 월별 청구액·납부·미확인 현황
# ─────────────────────────────────────────────────────────────────
def billing_report(db, month, cards, *, owner_id=None):
    """사용 중인 카드사의 입력 청구액·납부 전 금액·미확인 수를 계산합니다. 미입력과 0원을 구분합니다."""
    month_index(month)
    # 개인 API는 기본값을 사용하고, 공유 읽기 계층만 서버가 확인한 계정을 지정합니다.
    user_id = current_user_id() if owner_id is None else owner_id
    settings = {
        row["card"]: dict(row)
        for row in db.execute("SELECT * FROM billing_settings WHERE owner_id=?", (user_id,))
    }
    bills = {
        row["card"]: dict(row)
        for row in db.execute(
            "SELECT * FROM monthly_bills WHERE month=? AND owner_id=?", (month, user_id)
        )
    }
    year, number = map(int, month.split("-"))
    last = calendar.monthrange(year, number)[1]
    rows = []
    for entry in card_catalog(db, cards, owner_id=user_id):
        card = entry["card"]
        setting = settings.get(card, dict(enabled=1, due_day=None))
        row = bills.get(card, dict(statement_status="missing", amount=None, due_on=None, paid=0))
        default_due = f"{month}-{min(setting['due_day'],last):02}" if setting["due_day"] else None
        rows.append(
            dict(
                card=card,
                display_name=entry["display_name"],
                custom=entry["custom"],
                enabled=bool(setting["enabled"]),
                due_day=setting["due_day"],
                statement_status=row["statement_status"],
                amount=row["amount"],
                paid=bool(row["paid"]),
                due_on=row["due_on"] or default_due,
                due_override=row["due_on"],
            )
        )
    active = [row for row in rows if row["enabled"]]
    return dict(
        month=month,
        cards=rows,
        total=sum(row["amount"] or 0 for row in active),
        unpaid=sum(row["amount"] or 0 for row in active if not row["paid"]),
        missing_count=sum(row["statement_status"] == "missing" for row in active),
        unknown_count=sum(
            row["amount"] is None and row["statement_status"] != "no_bill" for row in active
        ),
    )


def validate_bill(payload, month):
    """월별 청구 기록을 검사합니다. 확인된 청구액 없이는 납부 완료로 표시하지 못합니다."""
    month_index(month)
    if not isinstance(payload, dict):
        raise ValueError("청구 내용을 확인해 주세요.")
    status = payload.get("statement_status")
    if status not in ("missing", "received", "no_bill"):
        raise ValueError("명세서 확인 상태를 선택해 주세요.")
    amount = payload.get("amount")
    if amount is not None:
        amount = integer(amount, "청구액", 0, 10**12)
    due = payload.get("due_on")
    if due is not None:
        try:
            valid = (
                isinstance(due, str)
                and date.fromisoformat(due).isoformat() == due
                and due[:7] == month
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("납부 예정일은 조회 월의 날짜로 입력해 주세요.")
    paid = payload.get("paid")
    if type(paid) is not bool:
        raise ValueError("납부 여부를 확인해 주세요.")
    if status == "no_bill" and (amount not in (None, 0) or paid):
        raise ValueError("청구 없음은 금액 없이 또는 0원으로 기록하고 납부 완료를 해제해 주세요.")
    if paid and (status != "received" or amount is None):
        raise ValueError("명세서 확인과 청구액 입력 후 납부 완료로 표시해 주세요.")
    return status, amount, due, int(paid)
