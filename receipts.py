"""거래 영수증 사진: 사용 내역 한 건에 장본 영수증 같은 사진을 붙여 두고 다시 봅니다.

저장 방식
  - 사진은 DB 안 transaction_receipts 표에 그대로 저장합니다. 그래서 매일 DB 백업(NAS 복사 포함)에
    함께 들어가고, 거래가 지워지면(명세서 삭제·직접 입력 삭제) 사진도 함께 지워집니다.
  - 화면(static/receipts.js)이 올리기 전에 긴 변 1600px JPEG로 줄여 위치 정보(EXIF)를 없앱니다.
    서버는 파일 앞부분 서명으로 JPEG·PNG·WebP만 받고, 그 형식으로만 돌려줍니다(SVG·HTML 거부).
  - 본인 거래에만 붙이고 지웁니다. 상대는 함께 보기(shared_spending.shared_receipt)로 지금 보이는
    거래의 사진만 읽기 전용으로 봅니다(숨김·제외 거래, 함께 보기를 끈 사람의 사진은 막힘).

호출 관계: app.py의 /api/transactions/<id>/receipts, /api/receipts/<id> → 이 파일
"""

MAX_BYTES = 3 * 1024 * 1024
MAX_PER_TRANSACTION = 5

SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


def sniff(data):
    """파일 앞부분으로 사진 형식을 정합니다. 허용하지 않는 형식이면 None입니다."""
    for signature, mime in SIGNATURES:
        if data.startswith(signature):
            return mime
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def owned(db, owner, transaction_id):
    return (
        db.execute(
            "SELECT 1 FROM transactions t JOIN imports i ON i.id=t.import_id "
            "WHERE t.id=? AND i.owner_id=?",
            (transaction_id, owner),
        ).fetchone()
        is not None
    )


def add(db, owner, transaction_id, data):
    """사진 한 장을 거래에 붙이고 새 사진 번호를 돌려줍니다."""
    if not owned(db, owner, transaction_id):
        raise LookupError("사용 내역을 찾지 못했습니다.")
    if not data:
        raise ValueError("사진 파일을 선택해 주세요.")
    if len(data) > MAX_BYTES:
        raise ValueError("사진이 너무 큽니다. 3MB 이하로 올려 주세요.")
    mime = sniff(data)
    if mime is None:
        raise ValueError("JPG·PNG·WebP 사진만 올릴 수 있어요.")
    count = db.execute(
        "SELECT COUNT(*) FROM transaction_receipts WHERE transaction_id=?", (transaction_id,)
    ).fetchone()[0]
    if count >= MAX_PER_TRANSACTION:
        raise ValueError(f"거래 한 건에 사진은 {MAX_PER_TRANSACTION}장까지 붙일 수 있어요.")
    cursor = db.execute(
        "INSERT INTO transaction_receipts(transaction_id,mime,data) VALUES(?,?,?)",
        (transaction_id, mime, data),
    )
    return cursor.lastrowid


def find(db, owner, receipt_id):
    """본인 거래에 붙은 사진이면 {transaction_id, mime, data}, 아니면 None입니다."""
    return db.execute(
        "SELECT r.transaction_id,r.mime,r.data FROM transaction_receipts r "
        "JOIN transactions t ON t.id=r.transaction_id JOIN imports i ON i.id=t.import_id "
        "WHERE r.id=? AND i.owner_id=?",
        (receipt_id, owner),
    ).fetchone()


def delete(db, owner, receipt_id):
    """사진 한 장을 지우고 그 사진이 붙어 있던 거래 번호를 돌려줍니다."""
    row = find(db, owner, receipt_id)
    if row is None:
        raise LookupError("사진을 찾지 못했습니다.")
    db.execute("DELETE FROM transaction_receipts WHERE id=?", (receipt_id,))
    return row["transaction_id"]
