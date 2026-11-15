"""계좌 출금을 사용자가 확인한 소비와 지출 아닌 이동으로 구분합니다.

은행 원문과 카드 원장은 그대로 보존합니다. 실제 지출로 확인한 출금만 개인 원장에
한 번 연결하며, 새 카드 중복 후보가 생기면 계좌 쪽 반영만 보류합니다. 외부 조회나
이체를 요청하지 않고 기존 요청별 권한·버전·트랜잭션 안에서 처리합니다.
"""

from datetime import date, timedelta
import hashlib
import json
import unicodedata

from flask import g, jsonify, request

from categories import category_catalog, merchant_key

SOURCE_PREFIX = "bank-expense:"
PAYMENT_LABEL = "우리은행 계좌"
DECISIONS = {
    "pending": "소비 미반영",
    "expense": "실제 지출",
    "card_payment": "카드 대금",
    "transfer": "내 계좌 이체",
    "topup": "충전",
    "card_duplicate": "카드 내역과 중복",
    "not_expense": "소비 미반영",
}


def schema(db):
    """은행 행과 확인한 소비의 연결만 추가합니다. 기존 출금은 자동 반영하지 않습니다."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS bank_expense_reviews(
          bank_transaction_id INTEGER PRIMARY KEY REFERENCES bank_transactions(id) ON DELETE CASCADE,
          decision TEXT NOT NULL CHECK(decision IN
            ('pending','expense','card_payment','transfer','topup','card_duplicate','not_expense')),
          ledger_transaction_id INTEGER UNIQUE REFERENCES transactions(id) ON DELETE SET NULL,
          matched_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
          acknowledged_candidates TEXT NOT NULL DEFAULT '[]',
          held INTEGER NOT NULL DEFAULT 0 CHECK(held IN (0,1)),
          reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS transactions_amount_date ON transactions(amount,used_on);
        """)


def bank_rows(db, owner, month=None, identifier=None, limit=None):
    """계좌·암호문 없이 본인 은행 행과 본인에게 연결된 원장 상태만 읽습니다."""
    sql = """SELECT b.id,b.link_id,b.used_on,b.used_time,b.incoming,b.outgoing,b.balance,
        b.description,COALESCE(r.decision,'pending') AS decision,COALESCE(r.held,0) AS held,
        r.ledger_transaction_id,r.matched_transaction_id,
        t.id AS expense_id,t.merchant AS expense_merchant,t.amount AS expense_amount,t.used_on AS expense_used_on,
        i.owner_id AS expense_owner,i.digest AS expense_source,
        a.category AS expense_category,COALESCE(a.actual_merchant,'') AS expense_actual,
        COALESCE(a.memo,'') AS expense_memo,e.transaction_id AS expense_excluded,
        CASE WHEN mi.owner_id=l.owner_id AND mi.digest NOT LIKE 'bank-expense:%'
          AND mt.amount=b.outgoing AND me.transaction_id IS NULL
          AND abs(julianday(mt.used_on)-julianday(b.used_on))<=3
          THEN 1 ELSE 0 END AS matched_present
        FROM bank_transactions b JOIN bank_links l ON l.id=b.link_id
        LEFT JOIN bank_expense_reviews r ON r.bank_transaction_id=b.id
        LEFT JOIN transactions t ON t.id=r.ledger_transaction_id AND EXISTS(
          SELECT 1 FROM imports own WHERE own.id=t.import_id AND own.owner_id=l.owner_id
          AND own.digest LIKE 'bank-expense:%')
        LEFT JOIN imports i ON i.id=t.import_id
        LEFT JOIN transaction_annotations a ON a.transaction_id=t.id
        LEFT JOIN transaction_exclusions e ON e.transaction_id=t.id
        LEFT JOIN transactions mt ON mt.id=r.matched_transaction_id
        LEFT JOIN imports mi ON mi.id=mt.import_id
        LEFT JOIN transaction_exclusions me ON me.transaction_id=mt.id
        WHERE l.owner_id=?"""
    args = [owner]
    if month is not None:
        sql += " AND substr(b.used_on,1,7)=?"
        args.append(month)
    if identifier is not None:
        sql += " AND b.id=?"
        args.append(identifier)
    sql += " ORDER BY b.used_on DESC,b.used_time DESC,b.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return [dict(row) for row in db.execute(sql, args)]


def review_state(row, owner):
    """본인 계좌·확인 상태·제외 여부를 모두 검사해 실제 집계 상태를 구합니다."""
    eligible = row["outgoing"] > 0 and row["incoming"] == 0
    present = bool(
        row["expense_id"]
        and row["expense_owner"] == owner
        and (row["expense_source"] or "").startswith(SOURCE_PREFIX)
    )
    active = bool(
        present
        and row["decision"] == "expense"
        and not row["held"]
        and row["expense_excluded"] is None
    )
    selected = bool(
        eligible and present and row["decision"] == "expense" and (active or row["held"])
    )
    # 선택하지 않은 출금은 할 일이 아닙니다. 선택 후 새 카드 후보로 보류된 건만 알립니다.
    pending = bool(selected and row["held"])
    if not eligible:
        label = "입금·금액 변동 없음"
    elif pending:
        label = "중복 확인 필요 · 반영 보류"
    elif row["decision"] == "expense":
        label = "소비 반영 중" if active else "소비 미반영"
    elif row["decision"] == "card_duplicate" and not row["matched_present"]:
        label = "소비 미반영"
    else:
        label = DECISIONS[row["decision"]]
    return dict(
        eligible=eligible, selected=selected, reflected=active, needs_review=pending, label=label
    )


def serialize_row(row, owner):
    """목록에는 허용한 은행 필드와 확인 상태만 반환합니다."""
    result = {
        key: row[key]
        for key in (
            "id",
            "link_id",
            "used_on",
            "used_time",
            "incoming",
            "outgoing",
            "balance",
            "description",
            "decision",
        )
    }
    result.update(review_state(row, owner))
    return result


def review_summary(db, owner, month=None):
    """선택·반영 건수와 중복 보류 월만 반환하며 미선택 출금을 확인 알림에 넣지 않습니다."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='bank_expense_reviews'").fetchone():
        return dict(
            pending_count=0,
            pending_month=None,
            reflected_count=0,
            selected_count=0,
            unselected_count=0,
        )
    pending, reflected, selected, unselected = [], 0, 0, 0
    for row in bank_rows(db, owner, month):
        state = review_state(row, owner)
        if state["needs_review"]:
            pending.append(row["used_on"][:7])
        reflected += int(state["reflected"])
        selected += int(state["selected"])
        unselected += int(state["eligible"] and not state["selected"])
    return dict(
        pending_count=len(pending),
        pending_month=min(pending) if pending else None,
        reflected_count=reflected,
        selected_count=selected,
        unselected_count=unselected,
    )


def candidates(db, owner, row):
    """같은 금액·앞뒤 3일의 본인 집계 대상 카드 내역을 후보로만 반환합니다."""
    used = date.fromisoformat(row["used_on"])
    found = [
        dict(item)
        for item in db.execute(
            """SELECT t.id,t.import_id,t.used_on,t.amount,t.merchant,i.card,
        COALESCE(NULLIF(a.actual_merchant,''),t.merchant) AS display_merchant
        FROM transactions t JOIN imports i ON i.id=t.import_id
        LEFT JOIN transaction_annotations a ON a.transaction_id=t.id
        LEFT JOIN transaction_exclusions e ON e.transaction_id=t.id
        WHERE i.owner_id=? AND i.digest NOT LIKE 'bank-expense:%'
          AND t.amount=? AND t.amount>0 AND t.used_on>=? AND t.used_on<=?
          AND e.transaction_id IS NULL ORDER BY abs(julianday(t.used_on)-julianday(?)),t.id""",
            (
                owner,
                row["outgoing"],
                (used - timedelta(days=3)).isoformat(),
                (used + timedelta(days=3)).isoformat(),
                row["used_on"],
            ),
        )
    ]
    signatures = []
    for item in found:
        identity = [
            item[key] for key in ("id", "import_id", "used_on", "amount", "merchant", "card")
        ]
        signatures.append(
            hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        )
    signatures.sort()
    token = hashlib.sha256(json.dumps(signatures).encode()).hexdigest()
    return found, signatures, token


def suggestion(description):
    """명시적인 단어 조합만 제안합니다. 제안으로 처리 상태나 원장을 바꾸지 않습니다."""
    key = merchant_key(description)
    if "카드대금" in key or "카드결제대금" in key:
        return "카드 대금 납부로 보입니다. 직접 확인해 주세요."
    if any(word in key for word in ("내계좌", "본인계좌", "자금이체")):
        return "본인 계좌 간 이동일 수 있습니다. 직접 확인해 주세요."
    wallets = (
        "카카오페이",
        "토스머니",
        "네이버페이",
        "선불",
        "지역화폐",
        "진천페이",
        "음성페이",
        "온누리",
    )
    if "충전" in key and any(word in key for word in wallets):
        return "지갑·지역화폐 충전일 수 있습니다. 실제 사용과 구분해 주세요."
    return "거래 설명과 카드 후보를 확인한 뒤 출금의 성격을 선택해 주세요."


def reconcile(db, owner):
    """새 카드 후보가 생기면 계좌 소비만 보류합니다. 자동 복원·카드 수정은 하지 않습니다."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='bank_expense_reviews'").fetchone():
        return 0
    rows = db.execute(
        """SELECT b.id,b.used_on,b.outgoing,r.ledger_transaction_id,r.acknowledged_candidates
        FROM bank_expense_reviews r JOIN bank_transactions b ON b.id=r.bank_transaction_id
        JOIN bank_links l ON l.id=b.link_id JOIN transactions t ON t.id=r.ledger_transaction_id
        JOIN imports i ON i.id=t.import_id
        WHERE l.owner_id=? AND i.owner_id=? AND i.digest LIKE 'bank-expense:%'
          AND r.decision='expense' AND r.held=0
          AND NOT EXISTS(SELECT 1 FROM transaction_exclusions e WHERE e.transaction_id=t.id)""",
        (owner, owner),
    ).fetchall()
    changed = 0
    for row in rows:
        _, current, _ = candidates(db, owner, row)
        try:
            known = json.loads(row["acknowledged_candidates"])
            if not isinstance(known, list) or any(not isinstance(item, str) for item in known):
                known = []
        except (TypeError, ValueError):
            known = []
        if set(current).difference(known):
            db.execute(
                "UPDATE bank_expense_reviews SET held=1 WHERE bank_transaction_id=?", (row["id"],)
            )
            db.execute(
                "INSERT OR IGNORE INTO transaction_exclusions(transaction_id) VALUES(?)",
                (row["ledger_transaction_id"],),
            )
            changed += 1
    return changed


def can_restore(db, transaction_id, owner):
    """일반 중복 복원 버튼으로 계좌 확인 상태를 우회하지 못하게 합니다."""
    row = db.execute(
        """SELECT r.decision,r.held FROM bank_expense_reviews r
        JOIN bank_transactions b ON b.id=r.bank_transaction_id JOIN bank_links l ON l.id=b.link_id
        WHERE r.ledger_transaction_id=? AND l.owner_id=?""",
        (transaction_id, owner),
    ).fetchone()
    if row is not None:
        return row["decision"] == "expense" and not row["held"]
    source = db.execute(
        "SELECT i.digest FROM transactions t JOIN imports i ON i.id=t.import_id WHERE t.id=? AND i.owner_id=?",
        (transaction_id, owner),
    ).fetchone()
    return source is None or not source[0].startswith(SOURCE_PREFIX)


def text_field(value, label, required=False):
    """사용자가 입력한 사용처·메모만 길이와 제어 문자를 검사합니다."""
    if (
        not isinstance(value, str)
        or len(value) > 500
        or any(unicodedata.category(char).startswith("C") and char not in "\n\t" for char in value)
    ):
        raise ValueError(label + "는 제어 문자 없이 500자 이하로 입력해 주세요.")
    value = value.strip()
    if required and not value:
        raise ValueError(label + "를 입력해 주세요.")
    return value


def save_review(db, owner, row, payload, *, edit_details=True):
    """확인·포함을 함께 저장합니다. 간단 선택은 기존 분류·메모를 덮어쓰지 않습니다."""
    decision = payload.get("decision")
    if not isinstance(decision, str) or decision not in DECISIONS:
        raise ValueError("출금의 성격을 선택해 주세요.")
    if not review_state(row, owner)["eligible"]:
        raise ValueError("입금과 금액 변동 없는 기록은 소비로 반영하지 않습니다.")
    ledger = row["ledger_transaction_id"]
    if ledger and (
        row["expense_owner"] != owner or not (row["expense_source"] or "").startswith(SOURCE_PREFIX)
    ):
        raise ValueError("연결된 지출을 확인하지 못했습니다. 화면을 다시 불러와 주세요.")
    if ledger and (
        row["expense_amount"] != row["outgoing"] or row["expense_used_on"] != row["used_on"]
    ):
        raise ValueError("연결된 지출의 날짜·금액이 달라 저장하지 않았습니다.")
    matched, acknowledged = None, []
    if decision in ("expense", "card_duplicate"):
        found, acknowledged, token = candidates(db, owner, row)
        if payload.get("candidate_token") != token:
            raise LookupError("카드 후보가 변경됐습니다. 최신 후보를 다시 불러와 주세요.")
        if decision == "card_duplicate":
            matched = payload.get("matched_transaction_id")
            if type(matched) is not int or matched not in {item["id"] for item in found}:
                raise ValueError("본인의 카드 중복 후보 중 하나를 선택해 주세요.")
        elif found and payload.get("confirm_separate") is not True:
            raise LookupError("카드 후보와 별개로 사용한 지출인지 확인해 주세요.")
    if decision == "expense":
        if edit_details:
            merchant = text_field(payload.get("merchant"), "사용처", required=True)
            memo = text_field(payload.get("memo", row["expense_memo"]), "메모")
            category = payload.get("category")
            if not isinstance(category, str) or category not in {
                item["key"] for item in category_catalog(db, owner)
            }:
                raise ValueError("내 소비 분야 목록에서 분야를 선택해 주세요.")
        else:
            # 새 선택은 분야 확인 전 상태로 만듭니다. 기존 연결의 입력값은 그대로 둡니다.
            merchant = text_field(row["description"][:500], "사용처") or "계좌 결제"
            memo, category = "", "미분류"
        created = not ledger
        if not ledger:
            month = row["used_on"][:7]
            digest = SOURCE_PREFIX + month
            db.execute(
                "INSERT OR IGNORE INTO imports(card,filename,digest,owner_id) VALUES(?,?,?,?)",
                (PAYMENT_LABEL, "계좌 지출 · " + month, digest, owner),
            )
            source = db.execute(
                "SELECT id FROM imports WHERE owner_id=? AND card=? AND digest=?",
                (owner, PAYMENT_LABEL, digest),
            ).fetchone()[0]
            ledger = db.execute(
                "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(?,?,?,?)",
                (source, row["used_on"], merchant, row["outgoing"]),
            ).lastrowid
        elif edit_details:
            db.execute("UPDATE transactions SET merchant=? WHERE id=?", (merchant, ledger))
        if created or edit_details:
            db.execute(
                "INSERT INTO transaction_annotations(transaction_id,category,actual_merchant,memo) VALUES(?,?,'',?) "
                "ON CONFLICT(transaction_id) DO UPDATE SET category=excluded.category,actual_merchant='',memo=excluded.memo",
                (ledger, category, memo),
            )
        if edit_details:
            db.execute(
                "INSERT OR IGNORE INTO category_reviews(transaction_id) VALUES(?)", (ledger,)
            )
        db.execute("DELETE FROM transaction_exclusions WHERE transaction_id=?", (ledger,))
    elif ledger:
        # 원장을 삭제하지 않아 사용자가 적은 분류·메모와 연결 ID를 보존합니다.
        db.execute(
            "INSERT OR IGNORE INTO transaction_exclusions(transaction_id) VALUES(?)", (ledger,)
        )
    db.execute(
        """INSERT INTO bank_expense_reviews(bank_transaction_id,decision,ledger_transaction_id,
        matched_transaction_id,acknowledged_candidates,held) VALUES(?,?,?,?,?,0)
        ON CONFLICT(bank_transaction_id) DO UPDATE SET decision=excluded.decision,
        ledger_transaction_id=excluded.ledger_transaction_id,matched_transaction_id=excluded.matched_transaction_id,
        acknowledged_candidates=excluded.acknowledged_candidates,held=0,reviewed_at=CURRENT_TIMESTAMP""",
        (row["id"], decision, ledger, matched, json.dumps(acknowledged)),
    )
    return ledger


def install(app, connect):
    """확인 폼과 저장은 본인 은행 행에만 허용하고 기존 개인 버전을 반드시 요구합니다."""

    @app.get("/api/bank/transactions/<int:identifier>/review")
    def bank_review_data(identifier):
        db = connect()
        try:
            rows = bank_rows(db, g.user["id"], identifier=identifier)
            if not rows:
                return jsonify(error="계좌 내역을 찾지 못했습니다."), 404
            row = rows[0]
            found, _, token = candidates(db, g.user["id"], row)
            return jsonify(
                transaction=serialize_row(row, g.user["id"]),
                merchant=row["expense_actual"] or row["expense_merchant"] or "",
                category=row["expense_category"],
                memo=row["expense_memo"],
                decisions=[dict(key=key, name=name) for key, name in DECISIONS.items()],
                categories=category_catalog(db, g.user["id"]),
                suggestion=suggestion(row["description"]),
                candidate_token=token,
                candidate_count=len(found),
                candidates=[
                    dict(
                        id=item["id"],
                        used_on=item["used_on"],
                        amount=item["amount"],
                        merchant=item["display_merchant"],
                        card=item["card"],
                    )
                    for item in found[:20]
                ],
                matched_transaction_id=(
                    row["matched_transaction_id"] if row["matched_present"] else None
                ),
            )
        finally:
            db.close()

    def update_review(identifier, *, selection=False):
        """두 저장 경로에 같은 소유권·버전·중복 검사를 적용합니다."""
        if request.headers.get("If-Match") is None:
            return (
                jsonify(
                    error="최신 계좌 화면을 불러온 뒤 저장해 주세요.", code="revision_required"
                ),
                428,
            )
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="출금 확인 내용을 입력해 주세요."), 400
        if selection:
            if type(payload.get("included")) is not bool:
                return jsonify(error="소비 반영 여부를 선택해 주세요."), 400
            payload = {
                "decision": "expense" if payload["included"] else "not_expense",
                "candidate_token": payload.get("candidate_token"),
                "confirm_separate": payload.get("confirm_separate"),
            }
        db = connect()
        try:
            rows = bank_rows(db, g.user["id"], identifier=identifier)
            if not rows:
                return jsonify(error="계좌 내역을 찾지 못했습니다."), 404
            try:
                save_review(db, g.user["id"], rows[0], payload, edit_details=not selection)
            except LookupError as error:
                return jsonify(error=str(error)), 409
            except ValueError as error:
                return jsonify(error=str(error)), 400
            g.audit_target = "계좌 출금 #" + str(identifier)
            return jsonify(ok=True)
        finally:
            db.close()

    @app.put("/api/bank/transactions/<int:identifier>/review")
    def bank_review_update(identifier):
        return update_review(identifier)

    @app.put("/api/bank/transactions/<int:identifier>/consumption")
    def bank_consumption_update(identifier):
        return update_review(identifier, selection=True)
