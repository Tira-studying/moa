"""해외(외화) 카드 결제의 원화 금액 확정입니다(2026-11-20 소유자 요청).

CODEF는 해외 결제의 이용금액을 외화(예: USD 12.34)로만 주고 원화 금액(resKRWAmt)은 비워 보냅니다.
그래서 수집할 때는 거래를 원화 0원으로 저장하고 외화 통화·금액을 이 표에 함께 남깁니다
(codef_sync.store_events → insert_transaction). 사용자가 사용 내역의 '상세·분류'에서
적용 환율이나 실제 원화 결제액을 넣으면 거래 금액을 그 원화로 바꿉니다(confirm).

- 원화가 정해지기 전에는 0원이라 합계·예산에 들어가지 않습니다. 화면에 '원화 입력 필요'로 표시합니다.
- 다시 조회해도 사용자가 정한 원화 금액은 덮어쓰지 않습니다(codef_sync.store_events가 확인).
- 거래 주인만 바꿀 수 있습니다. 거래가 지워지면 이 기록도 함께 지워집니다(ON DELETE CASCADE).
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

CURRENCY_PATTERN = re.compile(r"[A-Z]{3}")
FOREIGN_AMOUNT_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]{1,4})?")
RATE_PATTERN = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6})?")
MAX_KRW = 10_000_000_000


def clean_foreign_amount(value):
    """CODEF 외화 금액 문자열('1,234.50')을 쉼표 없는 양수 문자열로 바꿉니다. 아니면 None입니다."""
    if not isinstance(value, str):
        return None
    text = value.replace(",", "").strip()
    if not FOREIGN_AMOUNT_PATTERN.fullmatch(text) or Decimal(text) <= 0:
        return None
    return text


def record(db, transaction_id, currency, amount):
    """수집한 해외 결제의 통화·외화 금액을 원화 미확정 상태로 남깁니다."""
    db.execute(
        "INSERT OR IGNORE INTO foreign_amounts(transaction_id,currency,amount) VALUES(?,?,?)",
        (transaction_id, currency, amount),
    )


def krw_from(payload, foreign_amount):
    """입력(적용 환율 rate 또는 원화 결제액 krw 중 하나)으로 원화 금액과 기록할 환율을 정합니다.

    환율로 계산하면 원 단위에서 반올림합니다. 카드사 청구액과 1원 정도 다를 수 있으니
    정확한 금액을 알면 원화 결제액을 넣도록 화면에서 안내합니다.
    """
    if not isinstance(payload, dict):
        raise ValueError("적용 환율이나 원화 결제액을 입력해 주세요.")
    rate, krw = payload.get("rate"), payload.get("krw")
    if (rate in (None, "")) == (krw in (None, "")):
        raise ValueError("적용 환율과 원화 결제액 중 하나만 입력해 주세요.")
    if krw not in (None, ""):
        text = str(krw).replace(",", "").strip()
        if type(krw) is bool or not re.fullmatch(r"[0-9]{1,11}", text):
            raise ValueError("원화 결제액은 원 단위 숫자로 입력해 주세요.")
        amount = int(text)
        rate_text = ""
    else:
        rate_text = str(rate).replace(",", "").strip()
        if type(rate) is bool or not RATE_PATTERN.fullmatch(rate_text):
            raise ValueError("적용 환율은 숫자로 입력해 주세요(예: 1385.2).")
        try:
            value = Decimal(foreign_amount) * Decimal(rate_text)
        except InvalidOperation:
            raise ValueError("외화 금액을 확인하지 못했습니다.") from None
        amount = int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if not 0 < amount <= MAX_KRW:
        raise ValueError("원화 금액이 0원보다 커야 합니다.")
    return amount, rate_text


def confirm(db, owner, transaction_id, payload):
    """본인 해외 결제의 원화 금액을 정합니다. 바꾼 원화 금액을 돌려줍니다.

    입력: 쓰기 잠금을 연 DB 연결, 소유자, 거래 번호, {'rate': '1385.2'} 또는 {'krw': 17090}.
    """
    row = db.execute(
        "SELECT f.amount FROM foreign_amounts f JOIN transactions t ON t.id=f.transaction_id "
        "JOIN imports i ON i.id=t.import_id WHERE f.transaction_id=? AND i.owner_id=?",
        (transaction_id, owner),
    ).fetchone()
    if not row:
        raise LookupError("본인의 해외 결제 내역을 찾지 못했습니다.")
    amount, rate = krw_from(payload, row["amount"])
    db.execute("UPDATE transactions SET amount=? WHERE id=?", (amount, transaction_id))
    db.execute(
        "UPDATE foreign_amounts SET rate=?,confirmed=1 WHERE transaction_id=?",
        (rate, transaction_id),
    )
    return amount
