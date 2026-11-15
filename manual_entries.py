"""직접 입력 지출: 명세서·문자·계좌 연결로 들어오지 않는 현금·계좌이체·지역화폐·온누리상품권 지출을 손으로 적습니다.

저장 방식
  - 거래는 카드 명세서와 같은 transactions 표에 넣어 대시보드·예산·달력·소비 분석에 그대로 반영합니다.
  - 결제 수단·월마다 '저장 기록'(imports) 하나로 묶습니다. card는 '현금' 같은 결제 수단 이름,
    digest는 'manual:YYYY-MM'입니다. 이 접두어로 화면과 계산이 카드 거래와 구분합니다(payment_kind='manual').
  - 결제 수단·현금영수증 여부는 manual_entries 표(migrations.py 5단계)에 둡니다.
  - 분야·메모는 다른 거래와 같은 transaction_annotations에 저장하고, 사용자가 고른 분야이므로 확인 완료로 둡니다.

연말정산: 현금영수증을 받은 현금·계좌이체만 체크카드·현금영수증(30%)으로 계산합니다(tax_deduction.py).

호출 관계: app.py의 /api/transactions/manual* API → 이 파일의 검증·저장 함수
"""

import unicodedata
from datetime import date

SOURCE_PREFIX = "manual:"
METHODS = {
    "cash": "현금",
    "transfer": "계좌이체",
    "local": "지역화폐",
    "onnuri": "온누리상품권",
    "other": "기타 결제",
}
# 현금영수증은 현금과 계좌이체에만 발급됩니다. 지역화폐·온누리상품권은 앱·카드형이면 결제 수단 쪽에서
# 공제 자료가 따로 잡히고 종이형은 공제 대상이 아니어서, 모아에서는 공제 계산에 넣지 않습니다.
RECEIPT_METHODS = {"cash", "transfer"}
MAX_AMOUNT = 1_000_000_000


def text(value, label, limit, required=False):
    """사용처·메모처럼 받침 없는 이름(label)의 글자 입력을 검사합니다."""
    if not isinstance(value, str) or any(
        unicodedata.category(char).startswith("C") for char in value
    ):
        raise ValueError(f"{label}는 제어 문자 없이 입력해 주세요.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label}를 입력해 주세요.")
    if len(value) > limit:
        raise ValueError(f"{label}는 {limit}자 이하로 입력해 주세요.")
    return value


# 1. 입력 검사
# ─────────────────────────────────────────────────────────────────
def validate(payload, catalog):
    """날짜·사용처·금액·결제 수단·분야(이름)·메모·현금영수증 여부를 검사합니다.

    catalog는 categories.category_catalog 결과입니다. 화면은 분야 이름을 보내고, 저장은 분야 키로 합니다.
    """
    if not isinstance(payload, dict):
        raise ValueError("입력 값을 확인해 주세요.")
    used_on = payload.get("used_on")
    try:
        day = date.fromisoformat(used_on) if isinstance(used_on, str) else None
    except ValueError:
        day = None
    if not day or len(used_on) != 10 or not 2000 <= day.year <= 2100:
        raise ValueError("날짜를 확인해 주세요.")
    amount = payload.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount <= MAX_AMOUNT:
        raise ValueError("금액은 1원 이상 10억 원 이하의 정수로 입력해 주세요.")
    method = payload.get("method")
    if method not in METHODS:
        raise ValueError("결제 수단을 선택해 주세요.")
    names = {item["name"]: item["key"] for item in catalog}
    category = payload.get("category")
    if not isinstance(category, str) or category not in names:
        raise ValueError("내 분야 목록에서 분야를 선택해 주세요.")
    receipt = payload.get("cash_receipt", False)
    if type(receipt) is not bool:
        raise ValueError("현금영수증 여부를 확인해 주세요.")
    return {
        "used_on": used_on,
        "merchant": text(payload.get("merchant"), "사용처", 100, required=True),
        "amount": amount,
        "method": method,
        "category": names[category],
        "memo": text(payload.get("memo", ""), "메모", 500),
        "cash_receipt": receipt and method in RECEIPT_METHODS,
    }


# 2. 저장·수정·삭제
# ─────────────────────────────────────────────────────────────────
def source_import(db, owner, method, month):
    """결제 수단·월별 저장 기록을 찾거나 만듭니다."""
    label, digest = METHODS[method], SOURCE_PREFIX + month
    db.execute(
        "INSERT OR IGNORE INTO imports(card,filename,digest,owner_id) VALUES(?,?,?,?)",
        (label, f"직접 입력 · {month}", digest, owner),
    )
    return db.execute(
        "SELECT id FROM imports WHERE owner_id=? AND card=? AND digest=?", (owner, label, digest)
    ).fetchone()[0]


def drop_empty_imports(db, owner):
    """거래가 모두 빠진 직접 입력 저장 기록을 지워 저장 기록 목록에 0건이 남지 않게 합니다."""
    db.execute(
        "DELETE FROM imports WHERE owner_id=? AND digest LIKE ? AND NOT EXISTS("
        "SELECT 1 FROM transactions t WHERE t.import_id=imports.id)",
        (owner, SOURCE_PREFIX + "%"),
    )


def annotate(db, transaction_id, entry):
    db.execute(
        "INSERT INTO transaction_annotations(transaction_id,category,actual_merchant,memo) "
        "VALUES(?,?,'',?) ON CONFLICT(transaction_id) DO UPDATE SET category=excluded.category,"
        "actual_merchant='',memo=excluded.memo",
        (transaction_id, entry["category"], entry["memo"]),
    )
    db.execute(
        "INSERT OR IGNORE INTO category_reviews(transaction_id) VALUES(?)", (transaction_id,)
    )
    db.execute(
        "INSERT INTO manual_entries(transaction_id,method,cash_receipt) VALUES(?,?,?) "
        "ON CONFLICT(transaction_id) DO UPDATE SET method=excluded.method,"
        "cash_receipt=excluded.cash_receipt",
        (transaction_id, entry["method"], int(entry["cash_receipt"])),
    )


def create(db, owner, entry):
    source = source_import(db, owner, entry["method"], entry["used_on"][:7])
    transaction_id = db.execute(
        "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(?,?,?,?)",
        (source, entry["used_on"], entry["merchant"], entry["amount"]),
    ).lastrowid
    annotate(db, transaction_id, entry)
    return transaction_id


def owned(db, owner, transaction_id):
    """본인이 직접 입력한 거래인지 확인합니다. 카드·계좌 거래는 이 API로 고치거나 지울 수 없습니다."""
    return db.execute(
        "SELECT t.id FROM transactions t JOIN imports i ON i.id=t.import_id "
        "JOIN manual_entries m ON m.transaction_id=t.id "
        "WHERE t.id=? AND i.owner_id=? AND i.digest LIKE ?",
        (transaction_id, owner, SOURCE_PREFIX + "%"),
    ).fetchone()


def update(db, owner, transaction_id, entry):
    """날짜의 월이나 결제 수단이 바뀌면 해당 저장 기록으로 옮깁니다."""
    if not owned(db, owner, transaction_id):
        raise LookupError("직접 입력한 내역을 찾지 못했습니다.")
    source = source_import(db, owner, entry["method"], entry["used_on"][:7])
    db.execute(
        "UPDATE transactions SET import_id=?,used_on=?,merchant=?,amount=? WHERE id=?",
        (source, entry["used_on"], entry["merchant"], entry["amount"], transaction_id),
    )
    annotate(db, transaction_id, entry)
    drop_empty_imports(db, owner)


def delete(db, owner, transaction_id):
    if not owned(db, owner, transaction_id):
        raise LookupError("직접 입력한 내역을 찾지 못했습니다.")
    db.execute("DELETE FROM transactions WHERE id=?", (transaction_id,))
    drop_empty_imports(db, owner)
