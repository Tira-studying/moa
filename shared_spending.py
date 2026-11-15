"""서로의 사용 내역 함께 보기: 본인이 허용한 구성원의 한 달 사용 내역을 읽기 전용으로 만듭니다.

공개 범위
  - '내 사용 내역 함께 보기'(features.py의 share_transactions)는 기본 켜짐이고, 끈 사람의 내역은
    보이지 않습니다. 거래 상세에서 '상대에게 숨기기'(shared_hidden 표)를 고른 거래도 빠집니다.
  - 날짜·승인 시각·사용처(본인이 고친 이름 우선)·금액·분야·카드사·카드 끝 4자리·결제 방식·메모와
    영수증 사진 번호(receipts)만 허용 목록으로 복사합니다. 원본 명세서·파일명·거래 ID는 보내지 않습니다.
  - 영수증 사진은 shared_receipt()가 '지금도 함께 보기에 보이는 거래'의 사진인지 매번 다시 확인한 뒤
    돌려줍니다(2026-11-09 소유자 요청). 숨기거나 제외한 거래, 함께 보기를 끈 사람의 사진은 막힙니다.
  - timeline은 보는 사람과 공개한 구성원의 내역을 날짜·승인 시각순으로 합친 목록입니다.
  - 제외(중복·소비 아님)한 거래는 빼서 대시보드 통합 합계와 같은 기준으로 셉니다.
  - 수정 API는 없습니다. 상대 자료는 기존 개인 API의 소유권 검사로 계속 보호됩니다.

호출 관계: app.py의 GET /api/shared-transactions → report → static/shared-activity.js
"""

from categories import classify, category_labels
import bank_expenses
import features
import finance
import manual_entries

FEATURE = "share_transactions"


def payment_kind(digest):
    if digest.startswith(bank_expenses.SOURCE_PREFIX):
        return "bank"
    if digest.startswith(manual_entries.SOURCE_PREFIX):
        return "manual"
    return "card"


def member_rows(db, owner, month, own=False):
    """한 구성원의 해당 월 사용 내역을 허용 필드만으로 만듭니다(최근 날짜·시각 먼저).

    own=True는 보는 사람 자신의 내역입니다. 본인 자료이므로 '상대에게 숨기기' 거래도 넣고 hidden으로 표시합니다.
    """
    overrides = dict(
        db.execute(
            "SELECT merchant_key,category FROM merchant_categories WHERE owner_id=?", (owner,)
        ).fetchall()
    )
    labels = category_labels(db, owner)
    rows = []
    for row in db.execute(
        f"""SELECT t.used_on,t.used_time,t.amount,t.merchant,t.card_last_four,i.card,i.digest,
        a.category,a.actual_merchant,COALESCE(a.memo,'') AS memo,
        CASE WHEN h.transaction_id IS NULL THEN 0 ELSE 1 END AS hidden,
        COALESCE((SELECT GROUP_CONCAT(r.id) FROM transaction_receipts r
                  WHERE r.transaction_id=t.id), '') AS receipts
        FROM transactions t JOIN imports i ON i.id=t.import_id
        LEFT JOIN transaction_annotations a ON a.transaction_id=t.id
        LEFT JOIN transaction_exclusions e ON e.transaction_id=t.id
        LEFT JOIN shared_hidden h ON h.transaction_id=t.id
        WHERE i.owner_id=? AND e.transaction_id IS NULL
          {"" if own else "AND h.transaction_id IS NULL"}
          AND t.used_on>=? AND t.used_on<=?
        ORDER BY t.used_on DESC, t.used_time DESC, t.id DESC""",
        (owner, month + "-01", month + "-31"),
    ):
        merchant = row["actual_merchant"] or row["merchant"]
        category = row["category"] or classify(merchant, overrides)[0]
        item = dict(
            used_on=row["used_on"],
            used_time=row["used_time"],
            merchant=merchant,
            amount=row["amount"],
            category=labels.get(category, category),
            card=row["card"],
            last_four=row["card_last_four"],
            payment_kind=payment_kind(row["digest"] or ""),
            memo=row["memo"],
            receipts=sorted(int(value) for value in row["receipts"].split(",") if value),
        )
        if own:
            item["hidden"] = bool(row["hidden"])
        rows.append(item)
    return rows


def shared_receipt(db, viewer, receipt_id):
    """함께 보기에서 볼 수 있는 사진이면 {mime, data}, 아니면 None입니다.

    본인 사진이거나, 다른 활성 구성원이 함께 보기를 켜 두었고 그 거래를 숨기거나 제외하지 않았을 때만 허용합니다.
    """
    row = db.execute(
        """SELECT r.mime,r.data,i.owner_id,
        CASE WHEN h.transaction_id IS NULL AND e.transaction_id IS NULL THEN 1 ELSE 0 END AS visible
        FROM transaction_receipts r JOIN transactions t ON t.id=r.transaction_id
        JOIN imports i ON i.id=t.import_id JOIN users u ON u.id=i.owner_id AND u.active=1
        LEFT JOIN shared_hidden h ON h.transaction_id=t.id
        LEFT JOIN transaction_exclusions e ON e.transaction_id=t.id
        WHERE r.id=?""",
        (receipt_id,),
    ).fetchone()
    if row is None:
        return None
    if row["owner_id"] == viewer:
        return row
    if not row["visible"] or not features.is_enabled(db, row["owner_id"], FEATURE):
        return None
    return row


def summary(username, rows, **extra):
    return dict(
        username=username,
        total=sum(row["amount"] for row in rows),
        count=len(rows),
        **extra,
    )


def report(db, viewer, month):
    """구성원별 공개 여부·합계와, 나와 공개한 구성원의 내역을 날짜·시각순으로 합친 목록을 돌려줍니다."""
    finance.month_index(month)
    me = db.execute("SELECT username FROM users WHERE id=?", (viewer,)).fetchone()["username"]
    mine = member_rows(db, viewer, month, own=True)
    timeline = [dict(row, who=me, mine=True) for row in mine]
    members = []
    for user in db.execute(
        "SELECT id,username FROM users WHERE active=1 AND id<>? ORDER BY id", (viewer,)
    ).fetchall():
        if not features.is_enabled(db, user["id"], FEATURE):
            members.append(dict(username=user["username"], shared=False))
            continue
        rows = member_rows(db, user["id"], month)
        members.append(summary(user["username"], rows, shared=True, transactions=rows))
        timeline += [dict(row, who=user["username"], mine=False) for row in rows]
    # 같은 날은 승인 시각이 있는 거래를 시각순으로, 시각이 없는 명세서·직접 입력 거래는 그 뒤에 둡니다.
    timeline.sort(key=lambda row: (row["used_on"], row["used_time"]), reverse=True)
    return dict(
        month=month,
        readonly=True,
        sharing=features.is_enabled(db, viewer, FEATURE),
        me=summary(me, mine),
        members=members,
        timeline=timeline,
    )
