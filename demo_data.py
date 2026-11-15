"""처음 써 보는 사람을 위한 가상 데이터 채우기입니다(`python manage.py demo`).

실제 명세서 없이 화면을 둘러볼 수 있도록 가상 카드사 명세서(CSV)를 만들어
앱의 실제 저장 흐름(/api/preview → /api/import)으로 넣습니다. DB에 직접 쓰지 않으므로
실제 업로드와 같은 검증·분류·중복 차단을 거칩니다.

안전장치
- 이미 거래나 명세서가 있는 DB에는 아무것도 넣지 않고 멈춥니다(운영 DB 보호).
- 가맹점·금액은 모두 지어낸 값이며 고정 난수로 만들어 매번 같은 결과가 나옵니다.
- 로그인 비밀번호는 데모 전용 값으로 정하고 화면에 알려 줍니다. 운영 DB에서는 쓰지 마세요.
"""

from datetime import date, timedelta
import io
import random
import sqlite3
from pathlib import Path

DEMO_PASSWORD = "moa-demo-password"
HEADERS = {"X-Requested-With": "CardDashboard"}

# 카드사별 가상 가맹점과 금액 범위(원)입니다. 실제 상호가 아닌 지어낸 이름만 씁니다.
SPENDING = {
    "신한": [
        ("모아 식자재마트", 20000, 120000),
        ("동네 베이커리", 4000, 18000),
        ("모아 주유소", 40000, 90000),
        ("해피 약국", 3000, 25000),
    ],
    "삼성": [
        ("모아 슈퍼마켓", 15000, 90000),
        ("가상 카페", 4500, 12000),
        ("모아 분식", 7000, 22000),
        ("주말 영화관", 12000, 30000),
    ],
    "KB국민": [
        ("가상 백화점", 30000, 180000),
        ("모아 택시", 5000, 25000),
        ("가상 소아과의원", 8000, 40000),
        ("가상 학원", 150000, 300000),
    ],
}
# 매달 같은 날 같은 금액으로 결제되는 가상 정기 결제입니다(고정 지출 후보 화면 확인용).
MONTHLY = [
    ("삼성", "가상 음악 구독", 10900, 5),
    ("신한", "가상 통신요금", 55000, 20),
    ("KB국민", "가상 동영상 구독", 13500, 12),
]


def month_starts(today, count=3):
    """오늘이 속한 달부터 거슬러 count개 달의 1일을 오래된 순서로 돌려줍니다."""
    first = today.replace(day=1)
    months = []
    for _ in range(count):
        months.append(first)
        first = (first - timedelta(days=1)).replace(day=1)
    return list(reversed(months))


def statement_rows(card, today, rng):
    """한 카드사의 최근 3개월 가상 거래를 (이용일, 가맹점, 금액) 목록으로 만듭니다."""
    rows = []
    for start in month_starts(today):
        last_day = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        end = min(last_day, today)
        for _ in range(rng.randint(8, 14)):
            merchant, low, high = rng.choice(SPENDING[card])
            used_on = start + timedelta(days=rng.randint(0, (end - start).days))
            rows.append((used_on, merchant, rng.randint(low // 100, high // 100) * 100))
        for monthly_card, merchant, amount, day in MONTHLY:
            used_on = start.replace(day=day)
            if monthly_card == card and used_on <= today:
                rows.append((used_on, merchant, amount))
    # 취소·환불은 음수로 적습니다(합계에서 빠지는지 확인용).
    refund_day, merchant, amount = rows[0]
    rows.append((min(refund_day + timedelta(days=2), today), merchant, -amount))
    return sorted(rows)


def statement_csv(rows):
    """앱이 읽는 형식(제목 행 + 날짜·가맹점·금액)의 CSV 바이트를 만듭니다."""
    lines = ["이용일,가맹점,이용금액"]
    lines += [f"{used_on.isoformat()},{merchant},{amount}" for used_on, merchant, amount in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def database_is_empty(path):
    """거래·명세서가 하나도 없을 때만 True입니다. 표가 아직 없으면 새 DB로 봅니다."""
    if not Path(path).exists():
        return True
    db = sqlite3.connect(path)
    try:
        for table in ("transactions", "imports"):
            try:
                if db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    return False
            except sqlite3.OperationalError:
                continue
        return True
    finally:
        db.close()


def fill(today=None, seed=2026):
    """빈 DB에 가상 명세서 3개를 넣고 (카드사, 저장 건수) 목록을 돌려줍니다."""
    from app import app, connect
    from auth import set_password

    path = app.config["DATABASE"]
    if not database_is_empty(path):
        raise SystemExit(
            f"{path}에 이미 거래가 있어 가상 데이터를 넣지 않았습니다. "
            "CARD_DB=instance/demo.sqlite3 처럼 새 파일을 지정해 주세요."
        )
    today = today or date.today()
    rng = random.Random(seed)
    set_password(connect, DEMO_PASSWORD)
    client = app.test_client()
    login = client.post("/api/login", json={"password": DEMO_PASSWORD}, headers=HEADERS)
    if login.status_code != 200:
        raise SystemExit("데모 로그인에 실패했습니다. 앱 설정을 확인해 주세요.")
    saved = []
    for card in SPENDING:
        content = statement_csv(statement_rows(card, today, rng))
        preview = client.post(
            "/api/preview",
            data={"file": (io.BytesIO(content), f"demo-{card}.csv"), "header": "1"},
            headers=HEADERS,
        )
        if preview.status_code != 200:
            raise SystemExit(f"{card} 가상 명세서 미리보기에 실패했습니다: {preview.json}")
        result = client.post(
            "/api/import",
            json={
                "token": preview.json["token"],
                "card": card,
                "date": 0,
                "merchant": 1,
                "amount": 2,
            },
            headers=HEADERS,
        )
        if result.status_code != 200:
            raise SystemExit(f"{card} 가상 명세서 저장에 실패했습니다: {result.json}")
        saved.append((card, result.json["count"]))
    return saved
