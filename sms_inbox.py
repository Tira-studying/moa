"""아이폰 단축어가 전달한 결제 문자를 개인 임시내역으로 받습니다.

세션이 없는 수신 요청은 쓰기 전용 키를 DB 잠금 안에서 확인합니다. 키 원문은 발급 때만
보여주고 해시만 저장합니다. 문자 원문·이름·누적액은 보관하지 않으며, 임시내역은 명세서
원장이나 통합 합계에 더하지 않습니다. 검증한 KB국민·신한·삼성·현대카드 국내 원화 양식을 지원합니다.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
import sqlite3
import time

from flask import jsonify, request

import finance
from categories import merchant_key
from personal_data import current_user_id

KST = timezone(timedelta(hours=9))
MAX_TEXT = 2000


def schema(db):
    """기존 원장을 건드리지 않고 키와 개인 임시내역 표를 추가합니다."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sms_keys (
            user_id INTEGER PRIMARY KEY REFERENCES users(id), digest TEXT NOT NULL UNIQUE,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_received TEXT,
            window_start INTEGER NOT NULL DEFAULT 0, request_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sms_events (
            id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id),
            fingerprint TEXT NOT NULL, card TEXT NOT NULL, last_four TEXT NOT NULL,
            product TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL, amount INTEGER NOT NULL, occurred_at TEXT NOT NULL,
            merchant TEXT NOT NULL, payment TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(owner_id,fingerprint)
        );
        CREATE INDEX IF NOT EXISTS sms_events_owner_date ON sms_events(owner_id,occurred_at);
        """
    )
    # 기존 문자 DB에 상품명만 추가합니다. 기존 키·내역·중복 판정 해시는 그대로 보존합니다.
    if "product" not in {row[1] for row in db.execute("PRAGMA table_info(sms_events)")}:
        try:
            db.execute("ALTER TABLE sms_events ADD COLUMN product TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            # 앱·메일 작업자가 동시에 시작해 다른 연결이 먼저 추가했을 수 있습니다.
            if "product" not in {row[1] for row in db.execute("PRAGMA table_info(sms_events)")}:
                raise
        db.commit()


# 카드사별 문구와 줄 구성을 먼저 확인한 뒤 공통 필드로 변환합니다.
# 결제액에는 0이나 잘못된 쉼표를 허용하지 않고, 누적액에만 0을 허용합니다.
MONEY = r"(?:[1-9][0-9]{0,2}(?:,[0-9]{3})*|[1-9][0-9]*)"
PAYMENT = r"(?:일시불|할부\s*[0-9]{1,2}(?:개월)?|[0-9]{1,2}개월\s*할부)"
STAMP = r"[0-9]{2}/[0-9]{2}\s+[0-9]{2}:[0-9]{2}"
SHINHAN = re.compile(
    rf"신한카드\((?P<last_four>[0-9]{{4}})\)(?P<action>승인|취소)\s+"
    rf"\S{{1,40}}\s+(?P<amount>-?{MONEY})원\((?P<payment>{PAYMENT})\)\s*"
    rf"(?P<stamp>{STAMP})\s+(?P<merchant>[^\n]+?)\s+누적(?:{MONEY}|0)원"
)
SAMSUNG = re.compile(
    rf"삼성(?P<last_four>[0-9]{{4}})(?P<action>승인|취소)[ \t]+[^\n]{{1,40}}\n"
    rf"(?P<amount>-?{MONEY})원[ \t]+(?P<payment>{PAYMENT})\n"
    rf"(?P<stamp>{STAMP})[ \t]+(?P<merchant>[^\n]+?)\n누적(?:{MONEY}|0)원"
)

# 현대의 첫 줄은 카드 번호 대신 상품명입니다. 이름 줄이나 상품명에서 끝자리를 추측하지 않습니다.
HYUNDAI = re.compile(
    rf"현대[ \t]+(?P<product>\S[^\n]{{0,59}}?)[ \t]+(?P<action>승인|취소)\n"
    rf"[^\n]{{1,40}}\n(?P<amount>-?{MONEY})원[ \t]+(?P<payment>{PAYMENT})\n"
    rf"(?P<stamp>{STAMP})\n(?P<merchant>[^\n]+?)\n누적(?:{MONEY}|0)원"
)

# 단축어 JSON 입력칸이 줄바꿈을 공백으로 바꾸는 경우에도 필드 경계를 확인합니다.
# 누적액을 마지막 구분자로 요구하므로 잘린 문자나 여러 거래를 임의로 분리하지 않습니다.
KB_FLAT = re.compile(
    rf"KB국민카드\s*(?P<last_four>[0-9]{{4}})\s*(?P<action>승인|취소)\s+"
    rf"\S{{1,40}}님\s+(?P<amount>{MONEY})원\s+(?P<payment>{PAYMENT})\s+"
    rf"(?P<stamp>{STAMP})\s+(?P<merchant>.+?)\s+누적\s*(?:{MONEY}|0)원"
)
SAMSUNG_FLAT = re.compile(
    rf"삼성(?P<last_four>[0-9]{{4}})(?P<action>승인|취소)\s+\S{{1,40}}\s+"
    rf"(?P<amount>-?{MONEY})원\s+(?P<payment>{PAYMENT})\s+"
    rf"(?P<stamp>{STAMP})\s+(?P<merchant>.+?)\s+누적(?:{MONEY}|0)원"
)
HYUNDAI_FLAT = re.compile(
    rf"현대\s+(?P<product>\S[^\n]{{0,59}}?)\s+(?P<action>승인|취소)\s+\S{{1,40}}\s+"
    rf"(?P<amount>-?{MONEY})원\s+(?P<payment>{PAYMENT})\s+"
    rf"(?P<stamp>{STAMP})\s+(?P<merchant>.+?)\s+누적(?:{MONEY}|0)원"
)


def message_lines(text):
    """본문 크기·인코딩을 확인하고 줄 앞뒤 공백과 선택적인 발신 표시만 제거합니다."""
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT:
        raise ValueError("문자 본문은 2,000자 이하의 텍스트로 보내 주세요.")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise ValueError("문자 본문의 텍스트 인코딩을 확인해 주세요.") from None
    # 발신 표시가 같은 줄에 붙어도 제거하지만 본문 중간의 표시는 그대로 두어 다중 문자를 거부합니다.
    body = re.sub(r"^\s*\[Web발신\]\s*", "", text, count=1)
    return [line.strip() for line in body.splitlines() if line.strip()]


def make_event(card, last_four, action, amount, payment, stamp, merchant, now=None, product=""):
    """이름·누적액 없이 공통 임시내역을 만듭니다. 취소 부호는 원문과 무관하게 한 번만 적용합니다."""
    if (
        not merchant
        or len(merchant) > 200
        or not merchant.isprintable()
        or re.fullmatch(r"누적\s*[\d,]+원", merchant)
    ):
        raise ValueError("문자의 가맹점 이름을 확인해 주세요.")
    if product and (len(product) > 60 or not product.isprintable()):
        raise ValueError("문자의 카드 상품명을 확인해 주세요.")
    value = int(amount.replace(",", ""))
    # 음수 승인은 양식 해석 오류일 수 있으므로 소비로 바꿔 저장하지 않습니다.
    if action == "승인" and value < 0:
        raise ValueError("승인 문자의 금액 부호를 확인해 주세요.")
    value = abs(value)
    if not 1 <= value <= 10_000_000_000:
        raise ValueError("결제 금액 범위를 확인해 주세요.")
    now = (now or datetime.now(KST)).astimezone(KST)
    match = re.fullmatch(r"([0-9]{2})/([0-9]{2})\s+([0-9]{2}):([0-9]{2})", stamp)
    if not match:
        raise ValueError("문자의 이용일과 시각을 확인해 주세요.")
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(datetime(year, *map(int, match.groups()), tzinfo=KST))
        except ValueError:
            pass
    if not candidates:
        raise ValueError("문자의 이용일과 시각을 확인해 주세요.")
    occurred = min(candidates, key=lambda item: abs(item - now))
    if not -timedelta(minutes=5) <= now - occurred <= timedelta(days=7):
        raise ValueError(
            "최근 7일 이내 문자만 받을 수 있습니다. 과거 내역은 명세서로 등록해 주세요."
        )
    event = dict(
        card=card,
        last_four=last_four,
        kind="approval" if action == "승인" else "cancellation",
        amount=value if action == "승인" else -value,
        occurred_at=occurred.isoformat(timespec="minutes"),
        merchant=merchant,
        payment=payment,
    )
    # 빈 상품명을 기존 이벤트에 넣으면 기존 재전송 해시가 달라지므로 현대에만 포함합니다.
    if product:
        event["product"] = product
    return event


def event_from_match(card, match, now):
    """일치한 필드만 공통 검증으로 넘깁니다. 이름과 누적액은 캡처·저장하지 않습니다."""
    fields = match.groupdict()
    if card == "현대":
        fields["last_four"] = ""
    if re.search(r"누적\s*[0-9,]+원", fields["merchant"]):
        raise ValueError("여러 문자를 합치지 말고 승인 또는 취소 한 건씩 보내 주세요.")
    return make_event(card=card, now=now, **fields)


def parse_flat(card, pattern, lines, now):
    """공백으로 바뀐 줄 구분을 허용하되, 누적액 한 개와 완전한 본문 한 건을 요구합니다."""
    body = " ".join(lines)
    if "[Web발신]" in body or len(re.findall(rf"누적\s*(?:{MONEY}|0)원", body)) != 1:
        return None
    match = pattern.fullmatch(body)
    return event_from_match(card, match, now) if match else None


def parse_kb(text, now=None):
    """원래 KB 줄 구성과 공백으로 이어진 단축어 입력을 모두 읽으며 기존 중복 판정 값을 유지합니다."""
    lines = message_lines(text)
    if len(lines) in (5, 6):
        header = re.fullmatch(r"KB국민카드\s*([0-9]{4})\s*(승인|취소)", lines[0])
        amount = re.fullmatch(rf"({MONEY})원\s+({PAYMENT})", lines[2])
        if (
            header
            and lines[1].endswith("님")
            and amount
            and re.fullmatch(STAMP, lines[3])
            and (len(lines) == 5 or re.fullmatch(r"누적\s*[0-9,]+원", lines[5]))
        ):
            return make_event(
                "KB국민", header[1], header[2], amount[1], amount[2], lines[3], lines[4], now
            )
    event = parse_flat("KB국민", KB_FLAT, lines, now)
    if event:
        return event
    raise ValueError(
        "KB 문자 본문 전체를 보내 주세요. 카드 끝자리·승인/취소·금액·시각·가맹점이 필요합니다."
    )


def parse_sms(text, now=None):
    """줄바꿈 유무와 무관하게 지원하는 한 건의 필드를 확인하고, 불완전하거나 여러 건이면 거부합니다."""
    lines = message_lines(text)
    if lines and lines[0].startswith("KB국민카드"):
        return parse_kb(text, now)
    for card, pattern, line_count in (
        ("신한", SHINHAN, 1),
        ("삼성", SAMSUNG, 4),
        ("현대", HYUNDAI, 6),
    ):
        if len(lines) == line_count:
            match = pattern.fullmatch("\n".join(lines))
            if match:
                return event_from_match(card, match, now)
    # 기존 줄 구성을 우선해 기존 상품명·가맹점·중복 해시를 유지합니다.
    for card, pattern in (("신한", SHINHAN), ("삼성", SAMSUNG_FLAT), ("현대", HYUNDAI_FLAT)):
        event = parse_flat(card, pattern, lines, now)
        if event:
            return event
    raise ValueError("지원하는 KB국민·신한·삼성·현대 국내 원화 승인·취소 양식인지 확인해 주세요.")


def fingerprint(event):
    """같은 문자의 재전송을 막습니다. 누적액·이름·행 앞뒤 공백은 중복 판단에 사용하지 않습니다."""
    return hashlib.sha256(
        json.dumps(event, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def report(db, month):
    """본인 임시내역과 명세서 대조 후보만 반환합니다. 후보는 자동 확정·차감하지 않습니다."""
    finance.month_index(month)
    owner = current_user_id()
    key = db.execute(
        "SELECT created,last_received FROM sms_keys WHERE user_id=?", (owner,)
    ).fetchone()
    # 자동 결합하지 않습니다. 이름·날짜·금액이 같은 정상 결제나 할부 청구일 수 있습니다.
    matches = Counter(
        (row["card"], row["used_on"], merchant_key(row["merchant"]), row["amount"])
        for row in db.execute(
            "SELECT i.card,t.used_on,t.merchant,t.amount FROM transactions t "
            "JOIN imports i ON i.id=t.import_id "
            "LEFT JOIN transaction_exclusions e ON e.transaction_id=t.id "
            "WHERE i.owner_id=? AND t.used_on LIKE ? AND e.transaction_id IS NULL",
            (owner, month + "%"),
        )
    )
    items = []
    for row in db.execute(
        "SELECT id,card,last_four,product,kind,amount,occurred_at,merchant,payment,status,created "
        "FROM sms_events WHERE owner_id=? AND occurred_at LIKE ? ORDER BY occurred_at DESC,id DESC",
        (owner, month + "%"),
    ):
        item = dict(row)
        item["statement_candidates"] = matches[
            (item["card"], item["occurred_at"][:10], merchant_key(item["merchant"]), item["amount"])
        ]
        items.append(item)
    # 대조 완료는 명세서와 비교했다는 상태입니다. 소비의 존재나 문자 기준 합계를 지우지 않습니다.
    active = [row for row in items if row["status"] != "ignored"]
    spending_approvals = sum(row["amount"] for row in active if row["amount"] > 0)
    spending_cancellations = -sum(row["amount"] for row in active if row["amount"] < 0)
    pending = [row for row in items if row["status"] == "pending"]
    approvals = sum(row["amount"] for row in pending if row["amount"] > 0)
    cancellations = -sum(row["amount"] for row in pending if row["amount"] < 0)
    return dict(
        month=month,
        key=dict(key) if key else None,
        items=items,
        spending=dict(
            count=len(active),
            approvals=spending_approvals,
            cancellations=spending_cancellations,
            net=spending_approvals - spending_cancellations,
            settled_count=sum(row["status"] == "settled" for row in active),
            ignored_count=len(items) - len(active),
        ),
        # 기존 미대조 응답 필드는 구버전 화면과의 호환용으로 유지합니다.
        pending_count=len(pending),
        approvals=approvals,
        cancellations=cancellations,
        net=approvals - cancellations,
    )


def install(app, connect):
    """로그인한 설정·목록 요청과 쓰기 전용 키로 인증하는 수신 API를 등록합니다."""

    @app.get("/api/sms")
    def sms_data():
        """본인 정보만 조회합니다. URL의 owner_id 등으로 다른 계정을 선택할 수 없습니다."""
        db = connect()
        try:
            return jsonify(report(db, request.args.get("month", "")))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        finally:
            db.close()

    @app.post("/api/sms/key")
    def sms_key_create():
        """키를 교체하고 원문은 이번 응답에서만 전달합니다. 이전 키는 즉시 사용할 수 없습니다."""
        raw = secrets.token_urlsafe(32)
        db = connect()
        try:
            db.execute(
                "INSERT INTO sms_keys(user_id,digest) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET digest=excluded.digest,created=CURRENT_TIMESTAMP,"
                "last_received=NULL,window_start=0,request_count=0",
                (current_user_id(), hashlib.sha256(raw.encode()).hexdigest()),
            )
            return jsonify(key=raw, endpoint="/api/sms/receive")
        finally:
            db.close()

    @app.delete("/api/sms/key")
    def sms_key_revoke():
        """이 계정의 단축어 전송만 중단하고 기존 임시내역은 보존합니다."""
        db = connect()
        try:
            db.execute("DELETE FROM sms_keys WHERE user_id=?", (current_user_id(),))
            return jsonify(ok=True)
        finally:
            db.close()

    @app.put("/api/sms/events/<int:event_id>")
    def sms_event_update(event_id):
        """사용자가 명세서를 확인한 뒤 대조 완료·제외하거나 임시 집계로 복원합니다."""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("status") not in (
            "pending",
            "settled",
            "ignored",
        ):
            return jsonify(error="대조 상태를 확인해 주세요."), 400
        db = connect()
        try:
            cursor = db.execute(
                "UPDATE sms_events SET status=? WHERE id=? AND owner_id=?",
                (payload["status"], event_id, current_user_id()),
            )
            if not cursor.rowcount:
                return jsonify(error="임시내역을 찾지 못했습니다."), 404
            return jsonify(ok=True)
        finally:
            db.close()

    @app.post("/api/sms/receive")
    def sms_receive():
        """수신 키는 쓰기만 허용합니다. 쿠키·소유자 인자를 신뢰하지 않고 잠금 안에서 검증합니다."""
        request.max_content_length = 8192
        authorization = request.headers.get("Authorization", "")
        if not re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", authorization):
            return jsonify(error="문자 수신 키를 확인해 주세요."), 401
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        db = connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            user = db.execute(
                "SELECT u.id,u.username,k.window_start,k.request_count FROM sms_keys k "
                "JOIN users u ON u.id=k.user_id WHERE k.digest=? AND u.active=1",
                (digest,),
            ).fetchone()
            if not user:
                return jsonify(error="문자 수신 키를 확인해 주세요."), 401
            now = int(time.time())
            count = user["request_count"] if now - user["window_start"] < 60 else 0
            if count >= 120:
                return jsonify(error="수신 요청이 많습니다. 잠시 뒤 다시 보내 주세요."), 429
            db.execute(
                "UPDATE sms_keys SET window_start=?,request_count=? WHERE user_id=?",
                (user["window_start"] if count else now, count + 1, user["id"]),
            )
            # 읽기·계정 관리 기능이 없는 전용 입력입니다. 원문은 JSON 처리 메모리에만 머뭅니다.
            payload = request.get_json(silent=True)
            try:
                event = parse_sms(payload.get("text") if isinstance(payload, dict) else None)
            except ValueError as exc:
                db.commit()
                return jsonify(error=str(exc)), 422
            cursor = db.execute(
                "INSERT OR IGNORE INTO sms_events(owner_id,fingerprint,card,last_four,kind,amount,"
                "occurred_at,merchant,payment,product) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    user["id"],
                    fingerprint(event),
                    event["card"],
                    event["last_four"],
                    event["kind"],
                    event["amount"],
                    event["occurred_at"],
                    event["merchant"],
                    event["payment"],
                    event.get("product", ""),
                ),
            )
            duplicate = not bool(cursor.rowcount)
            db.execute(
                "UPDATE sms_keys SET last_received=CURRENT_TIMESTAMP WHERE user_id=?", (user["id"],)
            )
            if not duplicate:
                db.execute(
                    "INSERT INTO personal_revisions(user_id,revision) VALUES(?,1) "
                    "ON CONFLICT(user_id) DO UPDATE SET revision=revision+1",
                    (user["id"],),
                )
                db.execute(
                    "INSERT INTO audit_log(actor_id,actor_name,action,target,data_owner_id) VALUES(?,?,?,?,?)",
                    (
                        user["id"],
                        user["username"],
                        "문자 임시내역 수신",
                        "개인 임시내역",
                        user["id"],
                    ),
                )
            db.commit()
            return jsonify(ok=True, duplicate=duplicate), 200 if duplicate else 201
        finally:
            db.rollback()
            db.close()
