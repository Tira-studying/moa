"""DB 구조 버전 관리: 테이블 생성과 예전 DB 변환을 번호 붙은 단계로 한 번씩만 실행합니다.

SQLite 파일 안의 `PRAGMA user_version` 값이 "어디까지 적용했는지"를 기록합니다.
app.connect()는 연결할 때마다 이 숫자만 확인하고(매우 가벼움), 최신보다 낮을 때만 남은 단계를 실행합니다.

호출 관계: app.connect() → migrate(db) → MIGRATIONS의 각 단계 함수

## 새 구조 변경을 추가하는 방법
1. 이 파일에 `def add_xxx(db):` 함수를 만듭니다. 이미 적용된 DB에서도 안전하게
   여러 번 실행될 수 있게 씁니다(예: 컬럼이 이미 있으면 건너뛰기 — `add_column()` 사용).
2. 함수를 MIGRATIONS 목록의 **맨 끝**에 설명과 함께 추가합니다.
3. 이미 배포된 단계의 순서를 바꾸거나 지우지 않습니다. 번호가 바뀌면 운영 DB가 단계를 건너뜁니다.

각 기능 파일의 schema() 함수는 1단계(기존 구조) 안에서만 실행됩니다. schema()를 고쳐도
이미 1단계를 지난 DB에는 반영되지 않으므로, 새 변경은 반드시 아래 목록에 단계로 추가합니다.
"""

import threading

import auth
import bank_expenses
import categories
import codef_bank
import codef_connection
import codef_direct
import codef_sync
import connection_setup
import finance
import personal_data
import sms_inbox


# 1. 단계 함수
# ─────────────────────────────────────────────────────────────────
def baseline(db):
    """1단계: 버전 관리 도입 전의 모든 구조를 준비합니다.

    각 schema()는 테이블 생성과 예전 형식 자료 변환(컬럼 추가, 소유자 연결 등)을 함께 합니다.
    버전 관리 도입 전의 DB는 모두 user_version=0이므로, 어떤 예전 DB든 이 단계에서 최신 형태가 됩니다.
    순서가 중요합니다. 기본 거래 테이블 → 분류·계산·계정 → 개인 소유권(owner_id) → 연결 기능 순입니다.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY, card TEXT NOT NULL, filename TEXT NOT NULL,
            digest TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(card, digest)
        );
        CREATE TABLE IF NOT EXISTS merchant_categories (
            merchant_key TEXT PRIMARY KEY, category TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY, import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
            used_on TEXT NOT NULL, merchant TEXT NOT NULL, amount INTEGER NOT NULL
        );
    """)
    categories.schema(db)
    finance.schema(db)
    auth.auth_schema(db)
    personal_data.schema(db)
    finance.card_schema(db)
    sms_inbox.schema(db)
    codef_connection.schema(db)
    codef_sync.schema(db)
    codef_direct.schema(db)
    codef_bank.schema(db)
    bank_expenses.schema(db)
    connection_setup.schema(db)


def transaction_import_index(db):
    """2단계: 거래를 명세서(import_id)로 찾는 색인을 추가합니다.

    /api/data의 JOIN, 명세서 삭제 시 거래 연쇄 삭제(ON DELETE CASCADE)가 이 열로 거래를 찾습니다.
    색인이 없으면 거래가 늘수록 매번 전체 거래를 훑습니다.
    """
    db.execute("CREATE INDEX IF NOT EXISTS transactions_import ON transactions(import_id)")


def card_products(db):
    """3단계: 카드 상품별 전월 실적을 계산하기 위한 구조를 추가합니다.

    - card_products: 본인 카드 상품(카드사·이름·끝자리·혜택별 실적 기준·실적 제외 분야)
    - transactions.card_last_four: 거래를 낸 카드 번호 끝 4자리. 전체 카드 번호는 저장하지 않습니다.
    - codef_events.last_four: 검토 후 저장하는 CODEF 거래도 끝자리를 잃지 않게 보관합니다.
    - imports.card_product_id: 카드 한 장짜리 명세서를 통째로 특정 카드에 연결할 때 씁니다.
    - user_features: 사용자별 화면 기능 켜기·끄기(카드 실적은 기본 꺼짐)
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS card_products (
            id INTEGER PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            issuer TEXT NOT NULL,
            name TEXT NOT NULL,
            last_fours TEXT NOT NULL DEFAULT '[]',
            benefits TEXT NOT NULL DEFAULT '[]',
            excluded_categories TEXT NOT NULL DEFAULT '[]',
            note TEXT NOT NULL DEFAULT '',
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS card_products_owner ON card_products(owner_id, issuer);
        CREATE TABLE IF NOT EXISTS user_features (
            user_id INTEGER NOT NULL,
            feature TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            PRIMARY KEY (user_id, feature)
        );
    """)
    add_column(db, "transactions", "card_last_four", "TEXT NOT NULL DEFAULT ''")
    add_column(db, "codef_events", "last_four", "TEXT NOT NULL DEFAULT ''")
    add_column(db, "imports", "card_product_id", "INTEGER")


def planning_tools(db):
    """4단계: 연말정산 카드 공제 설정과 분야별 월 예산을 저장합니다.

    - tax_settings: 사용자·연도별 총급여(추정)·공제 대상 자녀 수·카드사별 신용/체크 구분·모아 밖 사용액
    - budgets: 사용자별 분야 월 예산. category가 '__total__'이면 전체 예산입니다.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tax_settings (
            owner_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            salary INTEGER NOT NULL DEFAULT 0,
            children INTEGER NOT NULL DEFAULT 0,
            issuer_types TEXT NOT NULL DEFAULT '{}',
            adjustments TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (owner_id, year)
        );
        CREATE TABLE IF NOT EXISTS budgets (
            owner_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            amount INTEGER NOT NULL,
            PRIMARY KEY (owner_id, category)
        );
    """)


def manual_entries(db):
    """5단계: 직접 입력 지출(현금·계좌이체 등)의 결제 수단과 현금영수증 여부를 저장합니다.

    거래 자체는 transactions에 있고, 이 표는 직접 입력한 거래에만 한 줄씩 붙습니다(manual_entries.py).
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS manual_entries (
            transaction_id INTEGER PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
            method TEXT NOT NULL,
            cash_receipt INTEGER NOT NULL DEFAULT 0
        );
    """)


def spending_choices(db):
    """6단계: 소비에서 뺄 카드 거래와 상대에게 숨길 거래를 구분해 저장합니다.

    - transaction_exclusions.reason: 'duplicate'(기존 중복 제외, 기본값) 또는 'not_spending'
      (카드 대금 이체·회사 경비처럼 소비가 아닌 거래). 둘 다 모든 합계에서 빠집니다.
    - shared_hidden: 사용 내역 함께 보기에서 상대에게 보이지 않을 내 거래(shared_spending.py).
    """
    add_column(db, "transaction_exclusions", "reason", "TEXT NOT NULL DEFAULT 'duplicate'")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS shared_hidden (
            transaction_id INTEGER PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE
        );
    """)


def payment_times(db):
    """7단계: 카드 승인 시각(HH:MM)을 거래에 저장합니다. CODEF 자동 수집만 시각을 주며,
    명세서·직접 입력 거래는 빈 값입니다. 함께 보기에서 같은 날 거래를 시간순으로 놓는 데 씁니다."""
    add_column(db, "transactions", "used_time", "TEXT NOT NULL DEFAULT ''")


def receipt_photos(db):
    """8단계: 거래에 붙이는 영수증 사진(receipts.py). 거래가 지워지면 사진도 함께 지워집니다."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS transaction_receipts (
            id INTEGER PRIMARY KEY,
            transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
            mime TEXT NOT NULL,
            data BLOB NOT NULL,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS transaction_receipts_transaction
            ON transaction_receipts(transaction_id);
    """)


def remember_login(db):
    """9단계: '로그인 유지'로 만든 세션을 표시합니다(auth.py). 기존 세션은 0이라 8시간 그대로 끝납니다."""
    add_column(db, "sessions", "remember", "INTEGER NOT NULL DEFAULT 0")


# 순서가 곧 버전 번호입니다. 1번째 = user_version 1. 맨 끝에만 추가합니다.
MIGRATIONS = [
    ("버전 관리 도입 전의 기존 구조", baseline),
    ("거래의 명세서 연결 색인", transaction_import_index),
    ("카드 상품별 전월 실적", card_products),
    ("연말정산 카드 공제·분야별 예산", planning_tools),
    ("직접 입력 지출", manual_entries),
    ("소비 제외 사유·함께 보기 숨김", spending_choices),
    ("카드 승인 시각", payment_times),
    ("거래 영수증 사진", receipt_photos),
    ("로그인 유지", remember_login),
]
LATEST_VERSION = len(MIGRATIONS)


# 2. 도우미
# ─────────────────────────────────────────────────────────────────
def add_column(db, table, column, definition):
    """컬럼이 없을 때만 추가합니다. 새 단계에서 ALTER TABLE을 안전하게 쓰기 위한 도우미입니다.

    예: add_column(db, "imports", "note", "TEXT NOT NULL DEFAULT ''")
    """
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def current_version(db):
    """DB 파일에 기록된 적용 단계 번호입니다. 새 파일이나 버전 관리 전 파일은 0입니다."""
    return db.execute("PRAGMA user_version").fetchone()[0]


# 3. 적용
# ─────────────────────────────────────────────────────────────────
# 같은 프로세스의 여러 스레드가 동시에 같은 단계를 실행하지 않게 막습니다.
# 웹 서버와 수집 프로세스가 동시에 실행해도 각 단계는 여러 번 실행해도 안전하게 작성합니다.
migration_lock = threading.Lock()


def migrate(db, migrations=None):
    """아직 적용하지 않은 단계를 순서대로 실행하고, 단계마다 user_version을 올립니다.

    입력: 열린 sqlite3 연결. migrations는 테스트에서 목록을 바꿔 끼울 때만 넘깁니다.
    출력: 적용 후의 버전 번호.
    중간 단계가 실패하면 그 앞 단계까지만 기록되므로, 원인을 고친 뒤 다음 연결에서 이어서 실행합니다.
    """
    steps = MIGRATIONS if migrations is None else migrations
    if current_version(db) >= len(steps):
        return current_version(db)
    with migration_lock:
        version = current_version(db)
        for number in range(version + 1, len(steps) + 1):
            _, step = steps[number - 1]
            step(db)
            db.commit()
            # PRAGMA는 ? 자리표시자를 쓸 수 없습니다. number는 코드가 만든 정수라 안전합니다.
            db.execute(f"PRAGMA user_version = {int(number)}")
            db.commit()
        return current_version(db)
