"""개인 원장 소유권과 통합 합계를 관리합니다.

기존 자료는 소유자(1번)에게 보존합니다. 다른 계정의 명세서·거래를 조회하지 않고,
통합 API에서만 활성 구성원의 월별 총액·분야·카드사 합계를 계산합니다.
"""

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
import sqlite3

from flask import g, has_request_context
from categories import classify, category_labels

BACKGROUND_USER = ContextVar("moa_background_user", default=1)


def current_user_id():
    """HTTP 요청은 인증된 사용자, 수집 프로세스는 현재 처리 중인 사용자를 사용합니다."""
    if has_request_context() and getattr(g, "user", None):
        return g.user["id"]
    return BACKGROUND_USER.get()


@contextmanager
def personal_scope(user_id):
    """별도 메일 수집에서 계정 설정을 섞지 않도록 작업 범위를 잠시 지정합니다."""
    token = BACKGROUND_USER.set(user_id)
    try:
        yield
    finally:
        BACKGROUND_USER.reset(token)


# 컬럼 이름은 코드에 고정하며 사용자 입력으로 SQL 구조를 만들지 않습니다.
SCOPED_TABLES = {
    "imports": (
        "id INTEGER PRIMARY KEY,card TEXT NOT NULL,filename TEXT NOT NULL,"
        "digest TEXT NOT NULL,created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        "owner_id INTEGER NOT NULL DEFAULT 1,UNIQUE(owner_id,card,digest)",
        "id,card,filename,digest,created",
    ),
    "merchant_categories": (
        "merchant_key TEXT NOT NULL,category TEXT NOT NULL,owner_id INTEGER NOT NULL DEFAULT 1,"
        "PRIMARY KEY(owner_id,merchant_key)",
        "merchant_key,category",
    ),
    "recurring_decisions": (
        "card TEXT NOT NULL,merchant_key TEXT NOT NULL,status TEXT NOT NULL,"
        "owner_id INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(owner_id,card,merchant_key)",
        "card,merchant_key,status",
    ),
    "billing_settings": (
        "card TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,due_day INTEGER,"
        "owner_id INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(owner_id,card)",
        "card,enabled,due_day",
    ),
    "monthly_bills": (
        "month TEXT NOT NULL,card TEXT NOT NULL,statement_status TEXT NOT NULL DEFAULT 'missing',"
        "amount INTEGER,due_on TEXT,paid INTEGER NOT NULL DEFAULT 0,"
        "owner_id INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(owner_id,month,card)",
        "month,card,statement_status,amount,due_on,paid",
    ),
}


def schema(db):
    """소유권 없는 기존 표를 원자적으로 이전합니다. 거래 ID와 연결은 그대로 유지합니다."""
    if "owner_id" in {row[1] for row in db.execute("PRAGMA table_info(imports)")}:
        return
    # imports를 교체할 때 연결된 거래가 연쇄 삭제되지 않게, 이전 연결에만 FK 검사를 잠시 끕니다.
    # 복사·교체·외래 키 검증을 같은 트랜잭션으로 끝내고 오류면 원래 표로 되돌립니다.
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        # 동시에 시작한 다른 연결이 이미 이전했을 수도 있습니다.
        if "owner_id" not in {row[1] for row in db.execute("PRAGMA table_info(imports)")}:
            for table, (definition, columns) in SCOPED_TABLES.items():
                db.execute(f"CREATE TABLE {table}_personal ({definition})")
                db.execute(f"INSERT INTO {table}_personal({columns}) SELECT {columns} FROM {table}")
                db.execute(f"DROP TABLE {table}")
                db.execute(f"ALTER TABLE {table}_personal RENAME TO {table}")
            db.execute(
                "ALTER TABLE installment_plans ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 1"
            )
            db.execute(
                "CREATE TABLE personal_revisions(user_id INTEGER PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute("CREATE INDEX imports_owner ON imports(owner_id)")
            db.execute("ALTER TABLE audit_log ADD COLUMN data_owner_id INTEGER DEFAULT 1")
            db.execute(
                "UPDATE audit_log SET data_owner_id=NULL WHERE action IN ('공동관리자 계정 추가','공동관리자 접근·비밀번호 변경')"
            )
            # 이전 공유 화면에 남은 세션은 종료하고 새 개인 권한으로 다시 로그인하게 합니다.
            db.execute("DELETE FROM sessions")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise sqlite3.IntegrityError("개인 자료 이전 중 연결 검증에 실패했습니다.")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")


def aggregate_spending(db, month):
    """가맹점·메모·파일·계정별 금액 없이 통합 합계만 반환합니다."""
    from finance import month_index, month_name

    end = month_index(month)
    first = month_name(end - 5)
    trend = {
        month_name(index): {"month": month_name(index), "amount": 0, "count": 0}
        for index in range(end - 5, end + 1)
    }
    overrides = defaultdict(dict)
    for row in db.execute("SELECT owner_id,merchant_key,category FROM merchant_categories"):
        overrides[row["owner_id"]][row["merchant_key"]] = row["category"]
    totals = {"net": 0, "gross": 0, "refunds": 0, "count": 0}
    categories = defaultdict(int)
    cards = defaultdict(int)
    labels = {
        row[0]: category_labels(db, row[0])
        for row in db.execute("SELECT id FROM users WHERE active=1").fetchall()
    }
    rows = db.execute(
        """SELECT t.used_on,t.amount,t.merchant,i.card,i.owner_id,
        a.category,a.actual_merchant FROM transactions t JOIN imports i ON i.id=t.import_id
        JOIN users u ON u.id=i.owner_id
        LEFT JOIN transaction_annotations a ON a.transaction_id=t.id
        LEFT JOIN transaction_exclusions e ON e.transaction_id=t.id
        WHERE u.active=1 AND e.transaction_id IS NULL AND t.used_on>=? AND t.used_on<=?""",
        (first + "-01", month + "-31"),
    )
    for row in rows:
        key = row["used_on"][:7]
        trend[key]["amount"] += row["amount"]
        trend[key]["count"] += 1
        if key != month:
            continue
        amount = row["amount"]
        totals["net"] += amount
        totals["count"] += 1
        if amount < 0:
            totals["refunds"] -= amount
        elif amount > 0:
            totals["gross"] += amount
            name = (
                row["category"]
                or classify(row["actual_merchant"] or row["merchant"], overrides[row["owner_id"]])[
                    0
                ]
            )
            categories[labels[row["owner_id"]].get(name, name)] += amount
            cards[row["card"]] += amount
    return dict(
        month=month,
        **totals,
        categories=[
            dict(name=name, amount=amount)
            for name, amount in sorted(categories.items(), key=lambda pair: -pair[1])
        ],
        cards=[
            dict(name=name, amount=amount)
            for name, amount in sorted(cards.items(), key=lambda pair: -pair[1])
        ],
        trend=list(trend.values()),
    )
