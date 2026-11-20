"""우리은행 개인 입출금 조회를 카드 원장과 분리해 저장합니다.

CODEF 공용 요청 예산·저장 키·예약 시각을 재사용합니다. 연결 ID·계좌번호·추가 인증은
사용자가 자동 수집 저장에 동의한 뒤 암호화하며, 로그인/계좌 비밀번호 평문은 받지 않습니다.
계좌 출금은 카드 결제·이체·충전일 수 있어 카드 지출 합계에 자동 반영하지 않습니다.
"""

from datetime import datetime, timedelta
import hashlib
import hmac
import json
import re
import time
import uuid

from cryptography.fernet import Fernet, InvalidToken
from flask import g, jsonify, request
import bank_expenses

from codef_card import CardCheckError, failure_hint, missing_parameter_hint, two_way_challenge
from codef_connection import config_digest
from codef_sync import (
    AuthenticationRequired,
    BudgetError,
    KST,
    SyncError,
    check_collection_configuration,
    latest_slot,
    money,
    storage_key,
    touch_revision,
)


def schema(db):
    """기존 카드·명세서 테이블을 변경하지 않고 개인 은행 표만 추가합니다."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS bank_links(
          id TEXT PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id),
          account_tag TEXT NOT NULL,cipher TEXT NOT NULL,app_digest TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1,enabled INTEGER NOT NULL DEFAULT 1,
          state TEXT NOT NULL DEFAULT 'ready',message TEXT NOT NULL DEFAULT '',
          last_success INTEGER,last_attempt INTEGER,requested INTEGER NOT NULL DEFAULT 0,
          requested_done INTEGER NOT NULL DEFAULT 0,UNIQUE(owner_id,account_tag));
        CREATE TABLE IF NOT EXISTS bank_jobs(
          link_id TEXT NOT NULL REFERENCES bank_links(id),slot TEXT NOT NULL,
          state TEXT NOT NULL,created INTEGER NOT NULL,PRIMARY KEY(link_id,slot));
        CREATE TABLE IF NOT EXISTS bank_transactions(
          id INTEGER PRIMARY KEY,link_id TEXT NOT NULL REFERENCES bank_links(id),
          fingerprint TEXT NOT NULL,used_on TEXT NOT NULL,used_time TEXT NOT NULL,
          incoming INTEGER NOT NULL,outgoing INTEGER NOT NULL,balance INTEGER NOT NULL,
          description TEXT NOT NULL,UNIQUE(link_id,fingerprint));
        CREATE INDEX IF NOT EXISTS bank_transactions_date ON bank_transactions(link_id,used_on);
        """)


def registration_hint(data):
    """단일 우리은행 개인 등록 오류에서 코드·고정 안내·알려진 누락 키만 추출합니다.

    CF-04000 바깥 결과와 기관 세부 오류는 다릅니다. 다른 기관·업무·부분 성공·여러
    오류를 섞어 해석하지 않고 공급자 원문이나 userError 값도 반환하지 않습니다.
    """
    if not isinstance(data, dict) or data.get("successList"):
        return ""
    errors = data.get("errorList")
    if not isinstance(errors, list) or len(errors) != 1 or not isinstance(errors[0], dict):
        return ""
    entry = errors[0]
    if any(
        entry.get(key, default) != expected
        for key, default, expected in (
            ("organization", None, "0020"),
            ("businessType", "BK", "BK"),
            ("countryCode", "KR", "KR"),
            ("clientType", "P", "P"),
        )
    ):
        return ""
    code = entry.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"CF-[0-9]{5}", code) or code == "CF-00000":
        return ""
    hint = (
        missing_parameter_hint(entry) if code in ("CF-12401", "CF-12411") else failure_hint(entry)
    )
    # failure_hint의 알려진 카드용 고정 문구만 은행 명칭으로 바꿉니다. 원문은 사용하지 않습니다.
    return f" 기관 상세 코드: {code}. " + hint.replace("카드사 홈페이지", "우리은행 홈페이지")


def query(client, token, payload, operation="bank_transactions", answer=None):
    """은행 오류는 원문 없이 안내합니다. 명시적인 SMS 추가 인증만 최대 1회 이어갑니다."""
    body = dict(payload)
    for attempt in range(2):
        response = client.send(token, body, operation)
        result = response.get("result", {})
        code = result.get("code")
        challenge = two_way_challenge(response, "0020", business_type="BK")
        if code == "CF-00000" and challenge is None:
            return response.get("data")
        if challenge is not None:
            if answer is None:
                raise AuthenticationRequired(
                    "우리은행 추가 인증이 필요합니다. 자동 수집을 멈췄습니다."
                )
            if attempt or challenge.get("method") != "smsAuthNo":
                raise AuthenticationRequired(
                    "지원하지 않는 우리은행 추가 인증입니다. 반복 요청하지 않았습니다."
                )
            value = answer("smsAuthNo")
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,32}", value):
                raise SyncError("인증번호 형식을 확인해 주세요. 재전송하지 않았습니다.")
            body = {
                **payload,
                "smsAuthNo": value,
                "is2Way": True,
                "twoWayInfo": {
                    name: challenge[name]
                    for name in ("jobIndex", "threadIndex", "jti", "twoWayTimestamp")
                },
            }
            continue
        safe = code if isinstance(code, str) and re.fullmatch(r"CF-[0-9]{5}", code) else "확인 불가"
        if code == "CF-12200":
            raise CardCheckError(
                "우리은행 통신 연결 실패 응답입니다. CODEF가 대상 기관과의 네트워크·사이트 연결 확인을 안내합니다. "
                "로그인 오류로 단정하지 않으며 자동 재시도하지 않았습니다. [코드: CF-12200]",
                code="CF-12200",
            )
        if code == "CF-12701":
            raise CardCheckError(
                "우리은행 전문·페이지 변경 응답입니다. 로그인 오류로 단정하지 않으며 "
                "반복되면 CODEF 확인이 필요합니다. 자동 재시도하지 않았습니다. [코드: CF-12701]",
                code="CF-12701",
            )
        if operation == "register" and code == "CF-04000":
            detail = registration_hint(response.get("data"))
            raise CardCheckError(
                "우리은행 계정 등록에 실패했습니다. 비밀번호 오류로 단정하지 않으며 자동 재시도하지 않았습니다. "
                "[코드: CF-04000]" + (detail or " 기관 상세 코드를 확인하지 못했습니다."),
                code="CF-04000",
            )
        raise CardCheckError(
            f"우리은행 조회를 완료하지 못했습니다. 자동 재시도하지 않았습니다. [코드: {safe}] "
            + (
                missing_parameter_hint(result)
                if code in ("CF-12401", "CF-12411")
                else failure_hint(result)
            ),
            code=safe,
        )


def registration_id(data):
    """다른 기관이나 부분 성공을 우리은행 연결 성공으로 취급하지 않습니다."""
    success = data.get("successList") if isinstance(data, dict) else None
    if (
        not isinstance(success, list)
        or len(success) != 1
        or data.get("errorList")
        or not isinstance(success[0], dict)
        or any(
            success[0].get(key) != value
            for key, value in dict(
                organization="0020", businessType="BK", clientType="P", code="CF-00000"
            ).items()
        )
    ):
        raise SyncError(
            "우리은행 단일 계정 등록 성공을 확인하지 못했습니다. 기존 연결은 유지합니다."
            + registration_hint(data)
        )
    cid = data.get("connectedId")
    if not isinstance(cid, str) or not 1 <= len(cid) <= 4096 or not cid.isprintable():
        raise SyncError("우리은행 연결 ID 응답 형식을 확인하지 못했습니다.")
    return cid


def account_tag(key, account):
    """계좌 식별자는 전체 번호를 노출하지 않는 키 기반 해시로 만듭니다."""
    return hmac.new(key, ("bank-account:" + account).encode(), hashlib.sha256).hexdigest()


def woori_auth_fields(account, encrypted_pin, birth_date):
    """우리은행 아이디 로그인 필수 계좌 확인값을 구성합니다. PIN은 이미 RSA 암호화된 값입니다.

    은행별 로그인 명세에서 withdrawAccountNo/withdrawAccountPassword는 필수이며,
    birthDate는 YYYYMMDD입니다. 같은 조회 계좌를 본인 확인에도 사용해 중복 입력을 피합니다.
    이 필드는 로그인 확인용이며 이체 API를 호출하지 않습니다.
    """
    if (
        not isinstance(account, str)
        or not re.fullmatch(r"[0-9]{10,16}", account)
        or not isinstance(encrypted_pin, str)
        or not 1 <= len(encrypted_pin) <= 4096
        or not isinstance(birth_date, str)
        or not re.fullmatch(r"[0-9]{8}", birth_date)
    ):
        raise SyncError("우리은행 계좌 확인 정보와 생년월일 8자리 형식을 확인해 주세요.")
    try:
        datetime.strptime(birth_date, "%Y%m%d")
    except ValueError:
        raise SyncError("우리은행 생년월일 날짜를 확인해 주세요.") from None
    return dict(
        withdrawAccountNo=account, withdrawAccountPassword=encrypted_pin, birthDate=birth_date
    )


def transaction_payload(fields, start, end):
    """최초 검증과 무인 작업자가 동일한 우리은행 로그인 필수값을 보내도록 묶습니다."""
    return dict(
        organization="0020",
        connectedId=fields["connectedId"],
        account=fields["account"],
        accountPassword=fields["accountPassword"],
        startDate=start,
        endDate=end,
        orderBy="0",
        inquiryType="1",
        **woori_auth_fields(fields["account"], fields["accountPassword"], fields["birthDate"]),
    )


def normalize(data, account, start, end, key):
    """한 계좌의 날짜·시각·원화 금액을 전부 검증한 뒤 허용한 필드만 저장합니다."""
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict) or data.get("resAccount") != account:
        raise SyncError("요청한 계좌와 응답 계좌가 일치하지 않아 저장하지 않았습니다.")
    rows = data.get("resTrHistoryList")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or len(rows) > 10000:
        raise SyncError("우리은행 거래 목록 형식을 확인하지 못해 저장하지 않았습니다.")
    result, occurrences = [], {}
    for row in rows:
        try:
            date, clock = row["resAccountTrDate"], row["resAccountTrTime"]
            if not isinstance(date, str) or not re.fullmatch(r"[0-9]{8}", date):
                raise ValueError()
            if not isinstance(clock, str) or not re.fullmatch(r"[0-9]{6}", clock):
                raise ValueError()
            parsed = datetime.strptime(date + clock, "%Y%m%d%H%M%S")
            if not start <= date <= end:
                raise ValueError()
            incoming, outgoing, balance = (
                money(row[name])
                for name in ("resAccountIn", "resAccountOut", "resAfterTranBalance")
            )
            # 은행은 입출금이 모두 0인 기록도 반환합니다. 금액을 만들거나 버리지 않고
            # 그대로 보존하며, 음수 흐름과 입금·출금이 동시에 양수인 응답은 차단합니다.
            if incoming < 0 or outgoing < 0 or (incoming > 0 and outgoing > 0):
                raise ValueError()
            desc = [row.get("resAccountDesc" + str(i), "") for i in (1, 2, 3)]
            if any(not isinstance(v, str) or len(v) > 500 for v in desc):
                raise ValueError()
        except (KeyError, TypeError, ValueError, SyncError):
            raise SyncError(
                "우리은행 거래의 날짜·시각·금액 형식을 확인하지 못해 저장하지 않았습니다."
            ) from None
        # 설명 수정으로 동일 거래가 늘어나지 않게 날짜·시각·금액·잔액만 식별에 사용합니다.
        base = json.dumps([account, date, clock, incoming, outgoing, balance])
        occurrence = occurrences.get(base, 0)
        occurrences[base] = occurrence + 1
        fingerprint = hmac.new(
            key, (base + ":" + str(occurrence)).encode(), hashlib.sha256
        ).hexdigest()
        result.append(
            (
                fingerprint,
                parsed.strftime("%Y-%m-%d"),
                clock,
                incoming,
                outgoing,
                balance,
                " · ".join(dict.fromkeys(v.strip() for v in desc if v.strip())),
            )
        )
    return result


def store(db, link_id, items):
    """같은 조회를 반복해도 건수가 늘지 않으며 카드 원장에는 쓰지 않습니다."""
    before = db.total_changes
    db.executemany(
        "INSERT INTO bank_transactions(link_id,fingerprint,used_on,used_time,incoming,outgoing,balance,description) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(link_id,fingerprint) DO UPDATE SET description=excluded.description "
        "WHERE description<>excluded.description",
        [(link_id, *item) for item in items],
    )
    return db.total_changes - before


def save_verified(connect, owner, fields, settings, key, items, previous, *, guard=None):
    """조회 성공 뒤에만 인증정보·입출금·개인 버전을 원자적으로 저장합니다."""
    woori_auth_fields(fields["account"], fields["accountPassword"], fields["birthDate"])
    tag = account_tag(key, fields["account"])
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if guard is not None:
                guard(db)
            check_collection_configuration(key, config_digest(settings))
            if not db.execute("SELECT 1 FROM users WHERE id=? AND active=1", (owner,)).fetchone():
                raise SyncError("모아 계정 접근이 해제되어 저장하지 않았습니다.")
            current = db.execute(
                "SELECT * FROM bank_links WHERE owner_id=? AND account_tag=?", (owner, tag)
            ).fetchone()
            if (dict(current) if current else None) != previous:
                raise SyncError("처리 중 우리은행 연결 상태가 바뀌어 저장하지 않았습니다.")
            link_id = current["id"] if current else uuid.uuid4().hex
            bundle = dict(fields, owner=owner, link_id=link_id, app_digest=config_digest(settings))
            cipher = Fernet(key).encrypt(json.dumps(bundle).encode()).decode()
            now = int(time.time())
            db.execute(
                "INSERT INTO bank_links(id,owner_id,account_tag,cipher,app_digest,last_success,last_attempt) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET cipher=excluded.cipher,app_digest=excluded.app_digest,version=version+1,"
                "enabled=1,state='ready',message='',last_success=excluded.last_success,last_attempt=excluded.last_attempt",
                (link_id, owner, tag, cipher, config_digest(settings), now, now),
            )
            store(db, link_id, items)
            db.execute(
                "INSERT OR IGNORE INTO bank_jobs VALUES(?,?,'covered',?)",
                (link_id, latest_slot(), now),
            )
            touch_revision(db, owner, "우리은행 개인 연결·입출금 저장")
            return link_id
    finally:
        db.close()


def load_fields(link, key):
    """암호화 내용을 사용자·연결·앱 설정·계좌 해시와 다시 대조합니다."""
    try:
        value = json.loads(Fernet(key).decrypt(link["cipher"].encode()))
        if (
            value["owner"] != link["owner_id"]
            or value["link_id"] != link["id"]
            or value["app_digest"] != link["app_digest"]
            or account_tag(key, value["account"]) != link["account_tag"]
        ):
            raise ValueError()
        return {
            name: value[name] for name in ("connectedId", "account", "accountPassword", "birthDate")
        }
    except (InvalidToken, UnicodeError, ValueError, TypeError, KeyError):
        raise SyncError("우리은행 암호화 연결을 확인하지 못했습니다. 수집을 멈췄습니다.") from None


def collect(connect, client, link, slot):
    """예약·수동 조회는 회차별 한 번만 실행하고 응답 저장 직전 접근·버전을 재검사합니다."""
    db = connect()
    now = int(time.time())
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT l.* FROM bank_links l JOIN users u ON u.id=l.owner_id WHERE l.id=? AND l.enabled=1 AND u.active=1",
                (link["id"],),
            ).fetchone()
            if not current or current["version"] != link["version"]:
                return
            if not db.execute(
                "INSERT OR IGNORE INTO bank_jobs VALUES(?,?,'running',?)", (link["id"], slot, now)
            ).rowcount:
                return
    finally:
        db.close()
    state, message, items = "ready", "최근 30일 입출금 조회 완료", None
    try:
        settings, token = client.credentials()
        if config_digest(settings) != link["app_digest"]:
            raise SyncError("CODEF 앱 설정이 바뀌어 우리은행 수집을 멈췄습니다.")
        key = storage_key()
        fields = load_fields(link, key)
        today = datetime.now(KST).date()
        start, end = (today - timedelta(days=29)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
        data = query(client, token, transaction_payload(fields, start, end))
        items = normalize(data, fields["account"], start, end, key)
    except BudgetError as error:
        state, message = "quota", str(error)
    except AuthenticationRequired as error:
        state, message = "needs_auth", str(error)
    except CardCheckError as error:
        state = "error" if error.code in (None, "CF-12200", "CF-12701") else "needs_auth"
        message = str(error)
        if error.code in TRANSIENT_CODES:
            state, message = transient_failure(connect, link, slot, error.code)
    except SyncError as error:
        state, message = "error", str(error)
    except Exception:
        state, message = "error", "우리은행 처리를 완료하지 못했습니다. 자동 조회를 멈췄습니다."
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT l.* FROM bank_links l JOIN users u ON u.id=l.owner_id WHERE l.id=? AND l.enabled=1 AND u.active=1",
                (link["id"],),
            ).fetchone()
            if not current or current["version"] != link["version"]:
                db.execute(
                    "UPDATE bank_jobs SET state='discarded' WHERE link_id=? AND slot=?",
                    (link["id"], slot),
                )
                return
            if items is not None:
                try:
                    check_collection_configuration(key, link["app_digest"])
                except SyncError as error:
                    state, message, items = "error", str(error), None
            if items is not None and store(db, link["id"], items):
                touch_revision(db, link["owner_id"], "우리은행 입출금 자동 저장")
            db.execute(
                "UPDATE bank_links SET state=?,message=?,last_attempt=?,last_success=CASE WHEN ? THEN ? ELSE last_success END WHERE id=?",
                (state, message, now, items is not None, now, link["id"]),
            )
            # 일시 오류로 다음 회차에 다시 조회하는 경우는 'transient'로 남겨 연속 여부를 판단합니다.
            job_state = "transient" if state == "ready" and items is None else state
            db.execute(
                "UPDATE bank_jobs SET state=? WHERE link_id=? AND slot=?",
                (job_state, link["id"], slot),
            )
    finally:
        db.close()


# 통신 실패(CF-12200)·전문 변경(CF-12701)은 은행·CODEF 쪽 일시 문제인 경우가 많습니다.
TRANSIENT_CODES = ("CF-12200", "CF-12701")


def transient_failure(connect, link, slot, code):
    """일시 오류는 다음 회차에 한 번만 다시 조회하고, 바로 앞 회차도 같은 실패였으면 멈춥니다.

    예전에는 한 번만 실패해도 error로 멈춰, 2026-11-04 통신 오류 한 번 뒤 16일 동안 조회하지 않았습니다.
    로그인 오류가 아니므로 비밀번호 오류 누적 위험은 없고, 연속 실패는 멈춰 요청을 반복하지 않습니다.
    """
    db = connect()
    try:
        previous = db.execute(
            "SELECT state FROM bank_jobs WHERE link_id=? AND slot<>? AND state<>'covered' "
            "ORDER BY created DESC, rowid DESC LIMIT 1",
            (link["id"], slot),
        ).fetchone()
    finally:
        db.close()
    if previous and previous["state"] == "transient":
        return (
            "error",
            "우리은행 조회가 두 번 연속 일시 오류로 실패해 자동 조회를 멈췄습니다. "
            f"은행·CODEF 상태를 확인해 주세요. 자동 재시도하지 않습니다. [코드: {code}]",
        )
    return (
        "ready",
        "우리은행 연결이 일시적으로 실패해 이번 회차를 저장하지 못했습니다. "
        f"로그인 오류가 아니며, 다음 회차에 한 번 다시 조회합니다. [코드: {code}]",
    )


def tick(connect, client, now=None):
    """인증·통신 오류 연결은 반복하지 않습니다. 카드와 같은 단일 작업자에서 실행합니다."""
    db = connect()
    try:
        links = [
            dict(row)
            for row in db.execute(
                "SELECT l.* FROM bank_links l JOIN users u ON u.id=l.owner_id WHERE l.enabled=1 AND u.active=1 AND l.state IN ('ready','quota') ORDER BY l.id"
            )
        ]
    finally:
        db.close()
    for link in links:
        manual = link["requested"] > link["requested_done"]
        collect(
            connect,
            client,
            link,
            "manual:" + str(link["requested"]) if manual else latest_slot(now),
        )
        if manual:
            db = connect()
            try:
                with db:
                    db.execute(
                        "INSERT OR IGNORE INTO bank_jobs VALUES(?,?,'covered',?)",
                        (link["id"], latest_slot(now), int(time.time())),
                    )
                    db.execute(
                        "UPDATE bank_links SET requested_done=MAX(requested_done,?) WHERE id=?",
                        (link["requested"], link["id"]),
                    )
            finally:
                db.close()


def install(app, connect):
    """은행 상태·입출금은 본인에게만 제공하며 연결 ID·계좌번호·인증정보는 반환하지 않습니다."""

    @app.get("/api/bank")
    def bank_data():
        month = request.args.get("month", datetime.now(KST).strftime("%Y-%m"))
        if not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", month):
            return jsonify(error="조회 월을 확인해 주세요."), 400
        db = connect()
        try:
            links = [
                dict(row)
                for row in db.execute(
                    "SELECT id,enabled,state,message,last_success,last_attempt FROM bank_links WHERE owner_id=? ORDER BY id",
                    (g.user["id"],),
                )
            ]
            rows = bank_expenses.bank_rows(db, g.user["id"], month, limit=1001)
            heartbeat = db.execute("SELECT heartbeat FROM codef_worker_state WHERE id=1").fetchone()
            return jsonify(
                links=links,
                transactions=[
                    bank_expenses.serialize_row(row, g.user["id"]) for row in rows[:1000]
                ],
                truncated=len(rows) > 1000,
                month=month,
                review_summary=bank_expenses.review_summary(db, g.user["id"], month),
                all_review_summary=bank_expenses.review_summary(db, g.user["id"]),
                worker_running=bool(heartbeat and heartbeat[0] > time.time() - 600),
            )
        finally:
            db.close()

    @app.post("/api/bank/refresh")
    def bank_refresh():
        db = connect()
        try:
            with db:
                db.execute(
                    "UPDATE bank_links SET requested=requested+1 WHERE owner_id=? AND enabled=1 AND state='ready' AND requested=requested_done",
                    (g.user["id"],),
                )
            return jsonify(ok=True)
        finally:
            db.close()

    @app.post("/api/bank/pause")
    def bank_pause():
        value = request.get_json(silent=True) or {}
        link_id = value.get("id") if isinstance(value, dict) else None
        if not isinstance(link_id, str):
            return jsonify(error="중지할 연결을 확인해 주세요."), 400
        db = connect()
        try:
            with db:
                changed = db.execute(
                    "UPDATE bank_links SET enabled=0,version=version+1 WHERE id=? AND owner_id=? AND enabled=1",
                    (link_id, g.user["id"]),
                ).rowcount
                if not changed:
                    return jsonify(error="본인 연결을 찾지 못했습니다."), 404
            return jsonify(ok=True)
        finally:
            db.close()
