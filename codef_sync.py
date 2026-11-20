"""개인별 CODEF 연결·예약 작업·검증된 지출 저장을 담당합니다.

연결 ID는 별도 비밀 볼륨의 키로 암호화합니다. 명시적으로 설정한 현대 인증정보는
별도 파일 또는 웹 연결의 이중 암호문으로 보관하고 PIN 평문·로그인 비밀번호·인증번호·응답 원문은 저장하지 않습니다. 네트워크는 DB 잠금 밖에서 처리하고 저장 직전에
계정 활성·연결 버전을 다시 검사합니다. 모호한 취소·명세서 중복 후보는 검토로 남깁니다.
"""

import base64
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from flask import g, jsonify, request

from codef_card import CARD_OPTIONS, CardCheckError, post_codef, query_approvals
from codef_connection import configuration, config_digest, issue_token
import codef_card_auth as card_auth
import bank_expenses
import foreign_payments
from card_performance import last_four_of

KST = ZoneInfo("Asia/Seoul")
HOURS = (0, 8, 14, 20)
NORMALIZATION_VERSION = 3
AMOUNT_CONFLICT_REASON = "동일 승인 식별자의 이용금액 차이 확인 필요"
STATUS_CONFLICT_REASON = "동일 승인 식별자의 승인·거절 상태 확인 필요"
CANCEL_CONFLICT_REASON = "동일 승인·취소 묶음 확인 필요"
FOREIGN_REASON = "외화·통화 확인 필요"


class SyncError(Exception):
    """개인정보·공급자 원문이 없는 고정 오류만 전달합니다."""


class BudgetError(SyncError):
    """로컬 요청 예산에 도달하면 외부 요청 전에 중단합니다."""


class DataReviewError(SyncError):
    """로그인 실패가 아니라 조회 결과를 안전하게 저장할 수 없는 상태입니다."""


class AuthenticationRequired(SyncError):
    """기존 연결로는 추가 인증이 필요하며 자동으로 계속 요청하지 않습니다."""


def schema(db):
    """연결·작업·출처 표를 추가하고 기존 원장 자료는 변경하지 않습니다."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS codef_links(
          owner_id INTEGER NOT NULL REFERENCES users(id),card TEXT NOT NULL,cipher TEXT NOT NULL,
          app_digest TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,enabled INTEGER NOT NULL DEFAULT 1,
          state TEXT NOT NULL DEFAULT 'ready',message TEXT NOT NULL DEFAULT '',error_code TEXT NOT NULL DEFAULT '',
          last_success INTEGER,last_attempt INTEGER,requested INTEGER NOT NULL DEFAULT 0,
          requested_done INTEGER NOT NULL DEFAULT 0,
          card_auth_ref TEXT NOT NULL DEFAULT '',card_auth_cipher TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(owner_id,card));
        CREATE TABLE IF NOT EXISTS codef_jobs(
          id INTEGER PRIMARY KEY,owner_id INTEGER NOT NULL,card TEXT NOT NULL,slot TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'running',created INTEGER NOT NULL,
          UNIQUE(owner_id,card,slot));
        CREATE TABLE IF NOT EXISTS codef_budget(day TEXT PRIMARY KEY,requests INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS codef_worker_state(id INTEGER PRIMARY KEY CHECK(id=1),heartbeat INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS codef_migrations(name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS codef_events(
          id INTEGER PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id),card TEXT NOT NULL,
          fingerprint TEXT NOT NULL,used_on TEXT NOT NULL,merchant TEXT NOT NULL,
          original_amount INTEGER NOT NULL,net INTEGER,kind TEXT NOT NULL,
          state TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',quarantined INTEGER NOT NULL DEFAULT 0,
          transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
          UNIQUE(owner_id,card,fingerprint));
        CREATE INDEX IF NOT EXISTS codef_events_owner ON codef_events(owner_id,state);
    """)
    if "error_code" not in {row[1] for row in db.execute("PRAGMA table_info(codef_links)")}:
        try:
            db.execute("ALTER TABLE codef_links ADD COLUMN error_code TEXT NOT NULL DEFAULT ''")
            db.commit()
        except sqlite3.OperationalError:
            if "error_code" not in {row[1] for row in db.execute("PRAGMA table_info(codef_links)")}:
                raise
    if "card_auth_ref" not in {row[1] for row in db.execute("PRAGMA table_info(codef_links)")}:
        try:
            db.execute("ALTER TABLE codef_links ADD COLUMN card_auth_ref TEXT NOT NULL DEFAULT ''")
            db.commit()
        except sqlite3.OperationalError:
            if "card_auth_ref" not in {
                row[1] for row in db.execute("PRAGMA table_info(codef_links)")
            }:
                raise
    if "quarantined" not in {row[1] for row in db.execute("PRAGMA table_info(codef_events)")}:
        try:
            db.execute("ALTER TABLE codef_events ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0")
            db.commit()
        except sqlite3.OperationalError:
            if "quarantined" not in {
                row[1] for row in db.execute("PRAGMA table_info(codef_events)")
            }:
                raise
    if "card_auth_cipher" not in {row[1] for row in db.execute("PRAGMA table_info(codef_links)")}:
        try:
            db.execute(
                "ALTER TABLE codef_links ADD COLUMN card_auth_cipher TEXT NOT NULL DEFAULT ''"
            )
            db.commit()
        except sqlite3.OperationalError:
            if "card_auth_cipher" not in {
                row[1] for row in db.execute("PRAGMA table_info(codef_links)")
            }:
                raise
    # 구버전에서 내역 충돌을 인증 오류로 표시한 상태만 한 번 교정합니다. 금융 원장은 건드리지 않습니다.
    with db:
        if db.execute("INSERT OR IGNORE INTO codef_migrations VALUES('error-states-v1')").rowcount:
            db.execute(
                "UPDATE codef_links SET state='needs_review' WHERE state='needs_auth' AND message=?",
                ("같은 승인 식별자의 응답이 서로 다릅니다. 저장하지 않았습니다.",),
            )


def storage_key():
    """키는 DB와 분리한 읽기 전용 비밀 볼륨에서만 읽습니다."""
    try:
        path = Path(
            os.environ.get("MOA_CODEF_STORAGE_KEY_FILE", "/run/moa-secrets/codef-storage.key")
        )
        with path.open("rb") as stream:
            value = stream.read(129)
        if len(value) > 128:
            raise ValueError()
        Fernet(value.strip())
        return value.strip()
    except (OSError, ValueError):
        raise SyncError("CODEF 저장 암호화 키 설정을 확인해 주세요.") from None


def latest_slot(now=None):
    """중단 중 놓친 회차를 몰아서 실행하지 않고 한국시간의 가장 최근 회차만 반환합니다."""
    now = now or datetime.now(KST)
    now = now.astimezone(KST)
    hour = max(hour for hour in HOURS if hour <= now.hour)
    return now.strftime("%Y-%m-%d") + f"T{hour:02d}:00"


def daily_request_limit():
    """운영자가 정한 로컬 요청 상한을 읽습니다. 공급자의 무료량과는 별개입니다."""
    try:
        limit = int(os.environ.get("MOA_CODEF_DAILY_REQUEST_LIMIT", "60"))
        if not 1 <= limit <= 1000:
            raise ValueError()
    except ValueError:
        raise SyncError("CODEF 요청 예산 설정을 확인해 주세요.") from None
    return limit


def require_request_budget(connect, minimum=1):
    """입력 전에 필요한 로컬 예산이 남았는지 읽기만 합니다. 실제 호출 예약은 따로 합니다."""
    limit = daily_request_limit()
    day = datetime.now(KST).strftime("%Y-%m-%d")
    db = connect()
    try:
        row = db.execute("SELECT requests FROM codef_budget WHERE day=?", (day,)).fetchone()
        used = row[0] if row else 0
    finally:
        db.close()
    remaining = max(0, limit - used)
    if remaining < minimum:
        raise BudgetError(
            f"오늘 남은 로컬 CODEF 요청 예산은 {remaining}회입니다(사용 {used}/{limit}회). "
            f"이 단계에는 최소 {minimum}회가 필요해 중단합니다."
        )
    return dict(day=day, limit=limit, used=used, remaining=remaining)


def reserve_request(connect):
    """작업자의 OAuth·등록·추가 인증·승인 요청을 외부 전송 전에 공용 예산에 예약합니다."""
    limit = daily_request_limit()
    day = datetime.now(KST).strftime("%Y-%m-%d")
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT requests FROM codef_budget WHERE day=?", (day,)).fetchone()
            if row and row[0] >= limit:
                raise BudgetError("오늘의 로컬 CODEF 요청 예산을 사용했습니다.")
            db.execute(
                "INSERT INTO codef_budget VALUES(?,1) ON CONFLICT(day) DO UPDATE SET requests=requests+1",
                (day,),
            )
    finally:
        db.close()


class Client:
    """앱 토큰은 프로세스 메모리에서만 짧게 재사용합니다. 금융 요청은 자동 재시도하지 않습니다."""

    def __init__(self, connect):
        self.connect = connect
        self.token = None
        self.digest = None
        self.expires = 0

    def credentials(self):
        settings = configuration()
        digest = config_digest(settings)
        if digest != self.digest or time.time() >= self.expires:
            reserve_request(self.connect)
            self.token = issue_token(settings)
            self.digest = digest
            self.expires = time.time() + 3600
        return settings, self.token

    def send(self, token, payload, operation="approval"):
        reserve_request(self.connect)
        return post_codef(token, payload, operation)


def touch_revision(db, owner, action="CODEF 지출 자동 저장"):
    """원장 변경과 같은 저장 단위에서 개인 버전과 일반 이력만 갱신합니다."""
    bank_expenses.reconcile(db, owner)
    db.execute(
        "INSERT INTO personal_revisions VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1",
        (owner,),
    )
    db.execute(
        "INSERT INTO audit_log(actor_id,actor_name,action,target,data_owner_id) SELECT id,username,?,?,id FROM users WHERE id=?",
        (action, "개인 CODEF 처리", owner),
    )


def save_link(connect, owner, card, connected_id, settings, *, verifying=True):
    """연결 ID만 암호화합니다. 비밀번호·개인 본인 확인 정보는 받지 않습니다."""
    if (
        card not in CARD_OPTIONS
        or not isinstance(connected_id, str)
        or not 1 <= len(connected_id) <= 4096
        or not connected_id.isprintable()
    ):
        raise SyncError("CODEF 연결 ID 응답 형식을 확인하지 못했습니다.")
    cipher = Fernet(storage_key()).encrypt(connected_id.encode()).decode()
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM users WHERE id=? AND active=1", (owner,)).fetchone():
                raise SyncError("활성 모아 계정을 확인해 주세요.")
            db.execute(
                "INSERT INTO codef_links(owner_id,card,cipher,app_digest,state) VALUES(?,?,?,?,?) ON CONFLICT(owner_id,card) DO UPDATE SET cipher=excluded.cipher,app_digest=excluded.app_digest,version=version+1,enabled=1,state=excluded.state,message='',card_auth_cipher=''",
                (
                    owner,
                    card,
                    cipher,
                    config_digest(settings),
                    "verifying" if verifying else "ready",
                ),
            )
            touch_revision(db, owner, "CODEF 개인 연결 변경")
    finally:
        db.close()


def money(value):
    """잘못된 금액을 0으로 바꾸지 않고 원화 정수만 허용합니다."""
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not re.fullmatch(r"-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.0+)?", value)
    ):
        raise DataReviewError("승인 금액 형식 확인이 필요합니다. 저장하지 않았습니다.")
    result = int(Decimal(value.replace(",", "")))
    if abs(result) > 10_000_000_000:
        raise DataReviewError("승인 금액 범위 확인이 필요합니다. 저장하지 않았습니다.")
    return result


def foreign_amount(value):
    """해외 결제의 외화 금액은 소수점을 허용하되, 숫자 형식이 아니면 저장하지 않고 멈춥니다."""
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not re.fullmatch(r"-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", value)
    ):
        raise DataReviewError("승인 금액 형식 확인이 필요합니다. 저장하지 않았습니다.")


def normalize(data, card, start, end):
    """필수 식별자를 HMAC으로 변환하고 안전한 공통 필드만 남깁니다. 원문은 보관하지 않습니다."""
    rows = data if isinstance(data, list) else [data]
    if len(rows) > 10000 or any(not isinstance(row, dict) for row in rows):
        raise DataReviewError("승인 목록 구조 확인이 필요합니다. 저장하지 않았습니다.")
    result = {}
    observations = {}
    missing_shapes = set()
    secret = base64.urlsafe_b64decode(storage_key())
    for row in rows:
        date, stamp = row.get("resUsedDate"), row.get("resUsedTime")
        try:
            if (
                not isinstance(date, str)
                or not re.fullmatch(r"[0-9]{8}", date)
                or not isinstance(stamp, str)
                or not re.fullmatch(r"[0-9]{6}", stamp)
            ):
                raise ValueError()
            when = datetime.strptime(date + stamp, "%Y%m%d%H%M%S")
            if not start <= date <= end:
                raise ValueError()
        except (ValueError, TypeError):
            raise DataReviewError(
                "승인 날짜·조회 범위 확인이 필요합니다. 저장하지 않았습니다."
            ) from None
        merchant = row.get("resMemberStoreName")
        if (
            not isinstance(merchant, str)
            or not 1 <= len(merchant.strip()) <= 200
            or not merchant.isprintable()
        ):
            raise DataReviewError("가맹점 형식 확인이 필요합니다. 저장하지 않았습니다.")
        identifiers = [row.get("resCardNo"), row.get("resApprovalNo")]
        if any(
            not isinstance(v, str) or not 1 <= len(v) <= 128 or not v.strip() or not v.isprintable()
            for v in identifiers
        ):
            raise DataReviewError(
                "승인 식별자 누락으로 중복 여부를 확인할 수 없습니다. 저장하지 않았습니다."
            )
        kind = row.get("resCancelYN")
        if kind not in ("0", "1", "2", "3"):
            raise DataReviewError("취소 상태 확인이 필요합니다. 저장하지 않았습니다.")
        currency = row.get("resAccountCurrency")
        domestic = currency == "KRW" or (
            currency in (None, "") and row.get("resHomeForeignType") == "1"
        )
        foreign = None
        if not domestic:
            # 해외 결제는 이용금액이 외화(예: USD 12.34)이고 원화 금액(resKRWAmt)은 비어 옵니다.
            # 정상 승인은 원화 0원으로 저장하고 통화·외화 금액을 남겨, 사용자가 환율이나
            # 원화 결제액을 넣으면 그 원화로 바꿉니다(foreign_payments.py).
            # 외화 취소·부분취소는 원승인과 맞춰 볼 원화가 없어 계속 검토 목록에 남깁니다.
            foreign_amount(row.get("resUsedAmount"))
            amount, net, reason = 0, 0, FOREIGN_REASON
            value = foreign_payments.clean_foreign_amount(row.get("resUsedAmount"))
            if (
                kind == "0"
                and isinstance(currency, str)
                and foreign_payments.CURRENCY_PATTERN.fullmatch(currency)
                and value
            ):
                foreign, reason = (currency, value), ""
            elif kind == "3":
                reason = ""
        else:
            amount = money(row.get("resUsedAmount"))
            reason = "" if amount > 0 else "이용금액 부호 확인 필요"
            net = amount
        if not domestic:
            pass
        elif kind == "1":
            net = 0
            if row.get("resCancelAmount") and money(row["resCancelAmount"]) != amount:
                reason = "취소금액과 원승인 확인 필요"
        elif kind == "2":
            cancel = row.get("resCancelAmount")
            if not cancel:
                reason = "부분취소 금액 확인 필요"
            else:
                value = money(cancel)
                if not 0 < value <= amount:
                    reason = "부분취소 금액 확인 필요"
                else:
                    net = amount - value
        elif kind == "3":
            net = 0
        card_identity = re.sub(r"[-\s]", "", identifiers[0]).replace("x", "*").replace("X", "*")
        fingerprint = hmac.new(
            secret,
            json.dumps(
                [card, card_identity, identifiers[1].strip(), date, stamp], ensure_ascii=False
            ).encode(),
            hashlib.sha256,
        ).hexdigest()
        item = dict(
            fingerprint=fingerprint,
            used_on=when.strftime("%Y-%m-%d"),
            merchant=merchant.strip(),
            original_amount=abs(amount),
            net=None if reason else net,
            kind=kind,
            reason=reason,
            quarantined=0,
            # 카드 상품별 실적 구분용 끝 4자리만 남깁니다. 전체·가려진 카드 번호는 저장하지 않습니다.
            last_four=last_four_of(card_identity),
            used_time=when.strftime("%H:%M"),
        )
        if foreign:
            item["foreign_currency"], item["foreign_amount"] = foreign
        if not item["last_four"]:
            # 끝자리를 못 읽은 카드사의 가림 형식만 작업자 로그로 남깁니다(숫자는 9로 바꿔 실제 번호 없음).
            missing_shapes.add(re.sub(r"[0-9]", "9", card_identity)[:32])
        observed = observations.setdefault(fingerprint, {"amounts": set(), "kinds": set()})
        observed["amounts"].add(item["original_amount"])
        observed["kinds"].add(kind)
        if fingerprint in result and result[fingerprint] != item:
            previous = result[fingerprint]
            if all(previous[name] == item[name] for name in ("merchant", "used_on")):
                if len(observed["amounts"]) > 1:
                    # 이용금액은 원금이라고 보장되지 않습니다. 합계·최대값·마지막 행을
                    # 고르지 않고 0을 '알 수 없음' 메타데이터로만 사용합니다. 원장에는 넣지 않습니다.
                    result[fingerprint] = quarantine_item(
                        previous, AMOUNT_CONFLICT_REASON, unknown_amount=True
                    )
                elif "3" in observed["kinds"]:
                    result[fingerprint] = quarantine_item(previous, STATUS_CONFLICT_REASON)
                else:
                    result[fingerprint] = {
                        **previous,
                        "net": None,
                        "kind": "mixed",
                        "reason": CANCEL_CONFLICT_REASON,
                    }
                continue
            fields = {
                "kind": "취소 상태",
                "original_amount": "이용금액",
                "net": "순금액",
                "merchant": "가맹점",
                "reason": "검토 사유",
            }
            changed = [
                label for key, label in fields.items() if result[fingerprint][key] != item[key]
            ]
            raise DataReviewError(
                "같은 승인 식별자의 응답이 서로 다릅니다. 저장하지 않았습니다. 서로 다른 항목: "
                + ", ".join(changed)
            )
        result[fingerprint] = item
    if missing_shapes:
        print(
            f"CODEF {card}: 카드 끝 4자리를 읽지 못한 번호 형식 {sorted(missing_shapes)[:5]}",
            flush=True,
        )
    return list(result.values())


def quarantine_item(item, reason, *, unknown_amount=False):
    """충돌 묶음은 후속 단일 응답으로도 자동 확정하지 않도록 별도 표시를 남깁니다."""
    return {
        **item,
        "original_amount": 0 if unknown_amount else item["original_amount"],
        "net": None,
        "kind": "mixed",
        "reason": reason,
        "quarantined": 1,
    }


def insert_transaction(db, owner, card, item):
    """검증한 출처를 수동·예약 조회가 함께 쓰는 월별 묶음으로 개인 원장에 추가합니다."""
    label = CARD_OPTIONS[card][1].removesuffix("카드")
    month = item["used_on"][:7]
    digest = "codef:" + card + ":" + month
    db.execute(
        "INSERT OR IGNORE INTO imports(card,filename,digest,owner_id) VALUES(?,?,?,?)",
        (label, "CODEF 조회 · " + month, digest, owner),
    )
    # 같은 출처에 새 거래를 더할 때 구버전의 자동 생성 이름만 교정합니다.
    # 사용자가 별도로 붙인 이름이나 다른 소유자의 묶음은 변경하지 않습니다.
    db.execute(
        "UPDATE imports SET filename=? WHERE owner_id=? AND card=? AND digest=? AND filename=?",
        (
            "CODEF 조회 · " + month,
            owner,
            label,
            digest,
            "CODEF 자동 수집 · " + month,
        ),
    )
    source = db.execute(
        "SELECT id FROM imports WHERE owner_id=? AND card=? AND digest=?", (owner, label, digest)
    ).fetchone()[0]
    transaction = db.execute(
        "INSERT INTO transactions(import_id,used_on,merchant,amount,card_last_four,used_time) "
        "VALUES(?,?,?,?,?,?)",
        (
            source,
            item["used_on"],
            item["merchant"],
            item["net"],
            item.get("last_four") or "",
            item.get("used_time") or "",
        ),
    ).lastrowid
    if item.get("foreign_currency"):
        foreign_payments.record(db, transaction, item["foreign_currency"], item["foreign_amount"])
    return transaction


def store_events(db, owner, card, items):
    """재조회는 중복 저장하지 않고, 불확실한 취소·다른 출처 중복은 검토 목록에 남깁니다."""
    changed = False
    label = CARD_OPTIONS[card][1].removesuffix("카드")
    for item in items:
        item = dict(item)
        item.setdefault("quarantined", 0)
        old = db.execute(
            "SELECT * FROM codef_events WHERE owner_id=? AND card=? AND fingerprint=?",
            (owner, card, item["fingerprint"]),
        ).fetchone()
        if (
            old
            and item.get("foreign_currency")
            and old["state"] == "review"
            and old["reason"] == FOREIGN_REASON
            and old["transaction_id"] is None
        ):
            # v18에서 검토 목록에만 남긴 외화 승인은 이제 외화 금액과 함께 저장합니다.
            old = None
        automatic_decline = bool(
            old
            and old["state"] == "ignored"
            and old["kind"] == "3"
            and old["reason"] == ""
            and old["transaction_id"] is None
        )
        if old and item.get("last_four") and not old["last_four"]:
            # 끝자리 저장 전에 수집한 거래는 다시 조회될 때 끝자리만 채웁니다. 금액·상태는 바꾸지 않습니다.
            db.execute(
                "UPDATE codef_events SET last_four=? WHERE owner_id=? AND card=? AND fingerprint=?",
                (item["last_four"], owner, card, item["fingerprint"]),
            )
            if old["transaction_id"]:
                db.execute(
                    "UPDATE transactions SET card_last_four=? WHERE id=? AND card_last_four='' "
                    "AND import_id IN (SELECT id FROM imports WHERE owner_id=?)",
                    (item["last_four"], old["transaction_id"], owner),
                )
        if old and old["transaction_id"] and item.get("used_time"):
            # 시각 저장 전에 수집한 거래는 다시 조회될 때 시각만 채웁니다.
            db.execute(
                "UPDATE transactions SET used_time=? WHERE id=? AND used_time='' "
                "AND import_id IN (SELECT id FROM imports WHERE owner_id=?)",
                (item["used_time"], old["transaction_id"], owner),
            )
        if old and old["state"] == "ignored" and not automatic_decline:
            continue
        if old and old["quarantined"]:
            # 기존 관측 메타데이터와 원장 연결을 그대로 둡니다. 새 정상·취소·거절 한 행이
            # 와도 이미 불명확해진 동일 식별자의 금액을 자동으로 복구하거나 없애지 않습니다.
            item = quarantine_item(
                {
                    **item,
                    "original_amount": old["original_amount"],
                    "merchant": old["merchant"],
                    "used_on": old["used_on"],
                },
                old["reason"] or AMOUNT_CONFLICT_REASON,
            )
        elif item["quarantined"]:
            item = quarantine_item(item, item["reason"] or AMOUNT_CONFLICT_REASON)
        elif old and old["original_amount"] != item["original_amount"]:
            item = quarantine_item(item, AMOUNT_CONFLICT_REASON, unknown_amount=True)
        elif (
            old
            and (old["kind"] == "3" or item["kind"] == "3")
            and any(old[name] != item[name] for name in ("kind", "net", "reason"))
        ):
            item = quarantine_item(item, STATUS_CONFLICT_REASON)
        if (
            old
            and all(
                old[key] == item[key]
                for key in ("net", "kind", "original_amount", "merchant", "quarantined")
            )
            and not (old["state"] == "saved" and old["transaction_id"] is None)
        ):
            continue
        reason = item["reason"]
        transaction = old["transaction_id"] if old else None
        state = "saved"
        if reason:
            state = "review"
        elif item["kind"] == "3":
            state, reason = (
                ("review", "기존 승인과 거절 상태 확인 필요") if transaction else ("ignored", "")
            )
        elif old and transaction and item.get("foreign_currency"):
            # 외화 거래의 원장 금액은 사용자가 정한 원화입니다. 다시 조회해도 바꾸지 않습니다.
            state, reason = "saved", ""
        elif (
            old
            and transaction
            and old["original_amount"] == item["original_amount"]
            and old["merchant"] == item["merchant"]
        ):
            current = db.execute(
                "SELECT t.amount FROM transactions t JOIN imports i ON i.id=t.import_id "
                "WHERE t.id=? AND i.owner_id=?",
                (transaction, owner),
            ).fetchone()
            if current is None:
                transaction = None
                state, reason = "review", "이전에 저장한 원장 내역 확인 필요"
            elif item["net"] > current[0]:
                # event의 직전 값은 mixed 검토에서 None으로 바뀔 수 있으므로 원장 실제 금액과
                # 비교합니다. 취소 뒤 정상 행만 내려온 경우에도 취소·부분취소를 자동 취소하지 않습니다.
                state, reason = "review", "기존 원장보다 금액이 증가해 확인 필요"
            elif not db.execute(
                "UPDATE transactions SET amount=? WHERE id=? AND amount>=? "
                "AND import_id IN (SELECT id FROM imports WHERE owner_id=?)",
                (item["net"], transaction, item["net"], owner),
            ).rowcount:
                state, reason = "review", "저장 중 원장 변경 확인 필요"
        elif old:
            state, reason = "review", old["reason"] or "기존 출처와 변경 내역 확인 필요"
        elif item["kind"] == "1":
            state, reason = "review", "원승인 확인이 필요한 취소"
        elif (
            not item.get("foreign_currency")
            and db.execute(
                "SELECT 1 FROM transactions t JOIN imports i ON i.id=t.import_id WHERE i.owner_id=? AND i.card=? AND i.digest NOT LIKE 'codef:%' AND t.used_on=? AND t.merchant=? AND t.amount IN (?,?) LIMIT 1",
                (
                    owner,
                    label,
                    item["used_on"],
                    item["merchant"],
                    item["original_amount"],
                    item["net"],
                ),
            ).fetchone()
        ):
            state, reason = "review", "명세서와 중복 후보"
        else:
            transaction = insert_transaction(db, owner, card, item)
        db.execute(
            "INSERT INTO codef_events(owner_id,card,fingerprint,used_on,merchant,original_amount,net,kind,state,reason,transaction_id,quarantined,last_four) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,card,fingerprint) DO UPDATE SET net=excluded.net,kind=excluded.kind,state=excluded.state,reason=excluded.reason,transaction_id=excluded.transaction_id,quarantined=excluded.quarantined,last_four=CASE WHEN excluded.last_four<>'' THEN excluded.last_four ELSE last_four END",
            (
                owner,
                card,
                item["fingerprint"],
                item["used_on"],
                item["merchant"],
                item["original_amount"],
                item["net"],
                item["kind"],
                state,
                reason,
                transaction,
                item["quarantined"],
                item.get("last_four") or "",
            ),
        )
        changed = True
    return changed


def check_collection_configuration(initial_key, expected_digest):
    """조회에 쓴 키와 현재 설정이 같아야 기존 출처 식별자를 그대로 저장할 수 있습니다."""
    message = "처리 중 CODEF 키 설정이 바뀌어 저장하지 않았습니다."
    try:
        current_key = storage_key()
        current_digest = config_digest(configuration())
    except (SyncError, ValueError):
        # 설정 파일이 사라지거나 형식을 잃은 경우에도 경로·파일 내용은 노출하지 않습니다.
        raise SyncError(message) from None
    if not hmac.compare_digest(initial_key, current_key) or expected_digest != current_digest:
        raise SyncError(message)


# 카드사 화면·전문이 잠시 달라졌다는 응답입니다. 비밀번호·인증 오류가 아닙니다.
# 2026-11-12 00시 KB에서 받은 뒤 08시 이후 직접 조회는 바로 성공했습니다.
TRANSIENT_CODE = "CF-12701"


def transient_failure(link):
    """CF-12701은 다음 회차에 한 번만 다시 조회하고, 연속 두 번이면 멈춥니다.

    인증 필요(needs_auth)로 두면 정기 회차가 그 카드를 계속 건너뛰어 하루 넘게 비게 됩니다.
    같은 응답이 연속되면 일시적 문제가 아니므로 error로 멈춰 요청을 반복하지 않습니다.
    """
    if dict(link).get("error_code") == TRANSIENT_CODE:
        return (
            "error",
            "카드사 조회가 두 번 연속 같은 응답으로 실패해 자동 조회를 멈췄습니다. "
            "CODEF 확인이 필요합니다. 자동 재시도하지 않습니다. [코드: CF-12701]",
        )
    return (
        "ready",
        "카드사 응답이 일시적으로 달라 이번 회차를 저장하지 못했습니다. "
        "다음 회차에 한 번 다시 조회합니다. [코드: CF-12701]",
    )


# KB 소지 확인 반영 대기 후 다시 조회하는 회차 이름 꼬리표와 대기 시간입니다.
FOLLOW_UP = "-kb-follow-up"
FOLLOW_UP_DELAY = 40 * 60


class KbPossessionPending(Exception):
    """저장한 KB 소지 정보를 보낸 뒤에도 KB가 조회를 바로 허용하지 않은 경우입니다.

    2026-11-12~14 00시 KB는 소지 정보 조회에 CF-12701을 돌려줬고, 곧바로 보낸 카드 정보
    없는 조회도 CF-12108이었습니다. 하지만 40~80분 뒤나 아침의 카드 정보 없는 조회는
    소지 확인 요구 없이 바로 성공했습니다. 저장한 정보가 틀린 것이 아니라 KB가 늦게
    반영하는 것이므로 다시 입력하라고 안내하지 않고, 정해진 시간 뒤 한 번만 다시 봅니다.
    """

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def kb_pending_state(slot, code):
    """정기 회차라면 40분 뒤 한 번 더 조회하도록 ready로 두고, 그 재조회도 막히면 멈춥니다.

    재조회에서 다시 CF-12108이면 저장한 정보를 한 번 더 보내므로 KB 소지 정보 전송은
    하루 최대 두 번입니다. 그 뒤로는 needs_auth로 멈춰 비밀번호 오류가 쌓이지 않게 합니다.
    """
    if slot.endswith(FOLLOW_UP):
        return (
            "needs_auth",
            "저장한 KB 소지 확인 정보를 보냈지만 KB가 40분 뒤에도 조회를 허용하지 않아 멈췄습니다. "
            "계속되면 카드·계좌 연결에서 KB를 다시 연결해 주세요. [코드: " + code + "]",
            code,
        )
    return (
        "ready",
        "KB가 소지 확인을 반영하는 중입니다. 40분 뒤 한 번 다시 조회합니다. [코드: " + code + "]",
        code,
    )


def kb_follow_up_due(link, now_ts):
    """직전 정기 회차가 KB 반영 대기로 끝났고 40분이 지났으면 재조회 회차를 돌려줍니다."""
    return (
        link["card"] == "kb"
        and link["state"] == "ready"
        and link["error_code"] in ("CF-12108", TRANSIENT_CODE)
        and bool(link["last_attempt"])
        and now_ts - link["last_attempt"] >= FOLLOW_UP_DELAY
    )


def collect(connect, client, link, slot, challenge=None):
    """회차를 먼저 예약하고 네트워크 후 계정·연결 버전을 재검사하여 한 번만 저장합니다."""
    owner, card = link["owner_id"], link["card"]
    now = int(time.time())
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM codef_links l JOIN users u ON u.id=l.owner_id WHERE l.owner_id=? AND l.card=? AND l.version=? AND l.enabled=1 AND u.active=1",
                (owner, card, link["version"]),
            ).fetchone():
                return
            if not db.execute(
                "INSERT OR IGNORE INTO codef_jobs(owner_id,card,slot,created) VALUES(?,?,?,?)",
                (owner, card, slot, now),
            ).rowcount:
                return
    finally:
        db.close()
    state, message, items, error_code = (
        "ready",
        "조회 완료. 저장된 지출과 검토 목록을 확인하세요.",
        None,
        "",
    )
    try:
        settings, token = client.credentials()
        if config_digest(settings) != link["app_digest"]:
            raise SyncError("CODEF 앱 키가 변경됐습니다. 개인 연결을 다시 확인해 주세요.")
        initial_key = storage_key()
        try:
            connected = Fernet(initial_key).decrypt(link["cipher"].encode()).decode()
        except (InvalidToken, UnicodeError):
            raise SyncError(
                "연결 정보를 읽지 못했습니다. 암호화 키와 연결을 확인해 주세요."
            ) from None
        today = datetime.now(KST).date()
        start, end = (today - timedelta(days=29)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
        payload = dict(
            organization=CARD_OPTIONS[card][0],
            connectedId=connected,
            startDate=start,
            endDate=end,
            orderBy="0",
            inquiryType="1",
            memberStoreInfoType="0",
        )
        fields, auth_snapshot = card_auth.load(link, connected, settings, initial_key)
        # 현대는 매 조회에 넣고, KB 소지 정보는 카드사가 CF-12108로 요구할 때만 한 번 보냅니다.
        possession = fields if card == "kb" else {}
        if card != "kb":
            payload.update(fields)

        def require_auth(method):
            raise AuthenticationRequired(
                "추가 인증이 필요합니다. 계정의 카드·계좌 연결에서 다시 인증해 주세요."
            )

        try:
            try:
                data = query_approvals(
                    token, payload, challenge or require_auth, sender=client.send
                )
            except CardCheckError as error:
                if not possession or error.code != "CF-12108":
                    raise
                # 실패하면 needs_auth로 멈추고 다음 회차에 반복하지 않아 비밀번호 오류 누적을 막습니다.
                payload.update(possession)
                try:
                    data = query_approvals(
                        token, payload, challenge or require_auth, sender=client.send
                    )
                except CardCheckError as retry_error:
                    if retry_error.code not in ("CF-12108", TRANSIENT_CODE):
                        raise
                    # 소지 정보는 받아들였지만 KB가 바로 반영하지 않은 경우입니다(아래 설명).
                    raise KbPossessionPending(retry_error.code) from None
        finally:
            payload.clear()
            possession.clear()
        check_collection_configuration(initial_key, link["app_digest"])
        card_auth.check_unchanged(link, auth_snapshot)
        items = normalize(data, card, start, end)
    except KbPossessionPending as error:
        state, message, error_code = kb_pending_state(slot, error.code)
    except BudgetError as error:
        state, message = "quota", str(error)
    except DataReviewError as error:
        state, message = "needs_review", str(error)
    except AuthenticationRequired as error:
        state, message = "needs_auth", str(error)
    except card_auth.CardAuthError as error:
        state, message = "needs_auth", str(error)
    except CardCheckError as error:
        error_code = error.code or ""
        # CF-04000은 계정 등록 실패 안내이므로 비밀번호 오류로 단정하지 않습니다.
        state = "needs_auth" if error_code and error_code != "CF-04000" else "error"
        message = str(error)
        if error_code == TRANSIENT_CODE:
            state, message = transient_failure(link)
    except SyncError as error:
        state, message = "error", str(error)
    except Exception:
        state, message = "error", "조회 처리를 완료하지 못했습니다. 연결 상태를 확인해 주세요."
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT l.* FROM codef_links l JOIN users u ON u.id=l.owner_id WHERE l.owner_id=? AND l.card=? AND l.enabled=1 AND u.active=1",
                (owner, card),
            ).fetchone()
            if not current or current["version"] != link["version"]:
                db.execute(
                    "UPDATE codef_jobs SET state='discarded' WHERE owner_id=? AND card=? AND slot=?",
                    (owner, card, slot),
                )
                return
            if items is not None:
                try:
                    # 정규화와 DB 재연결 사이의 키 변경도 같은 쓰기 잠금 안에서 확인합니다.
                    check_collection_configuration(initial_key, link["app_digest"])
                    card_auth.check_unchanged(current, auth_snapshot)
                except (SyncError, card_auth.CardAuthError) as error:
                    state, message, items, error_code = "error", str(error), None, ""
            if items is not None and store_events(db, owner, card, items):
                touch_revision(db, owner)
            db.execute(
                "UPDATE codef_links SET state=?,message=?,error_code=?,last_attempt=?,last_success=CASE WHEN ? THEN ? ELSE last_success END WHERE owner_id=? AND card=?",
                (state, message, error_code, now, items is not None, now, owner, card),
            )
            db.execute(
                "UPDATE codef_jobs SET state=? WHERE owner_id=? AND card=? AND slot=?",
                ("done" if items is not None else state, owner, card, slot),
            )
    finally:
        db.close()


def tick(connect, client, now=None):
    """고정 회차와 본인 수동 요청을 처리합니다. 인증 대기 카드는 자동 반복하지 않습니다."""
    db = connect()
    try:
        with db:
            db.execute(
                "INSERT INTO codef_worker_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat",
                (int(time.time()),),
            )
        links = [
            dict(row)
            for row in db.execute(
                "SELECT l.* FROM codef_links l JOIN users u ON u.id=l.owner_id WHERE l.enabled=1 AND u.active=1 AND l.state IN ('ready','quota') ORDER BY l.owner_id,l.card"
            )
        ]
    finally:
        db.close()
    for link in links:
        # 한 카드 조회가 5분 걸려도 작업자 상태를 갱신하며, 수동 완료 표시는 시각 비교와 분리합니다.
        db = connect()
        try:
            with db:
                db.execute(
                    "UPDATE codef_worker_state SET heartbeat=? WHERE id=1", (int(time.time()),)
                )
        finally:
            db.close()
        manual = link["requested"] > link["requested_done"]
        if manual:
            slot = "manual:" + str(link["requested"])
        elif kb_follow_up_due(link, int(time.time())):
            # 회차 이름이 고유하므로 같은 정기 회차의 재조회는 한 번만 실행됩니다.
            slot = latest_slot(now) + FOLLOW_UP
        else:
            slot = latest_slot(now)
        collect(connect, client, link, slot)
        if manual:
            # 같은 회차의 정기·수동 요청이 연이어 외부 조회되지 않도록 현재 회차도 처리 표시합니다.
            db = connect()
            try:
                with db:
                    db.execute(
                        "INSERT OR IGNORE INTO codef_jobs(owner_id,card,slot,state,created) VALUES(?,?,?,'covered',?)",
                        (link["owner_id"], link["card"], latest_slot(now), int(time.time())),
                    )
                    db.execute(
                        "UPDATE codef_links SET requested_done=MAX(requested_done,?) WHERE owner_id=? AND card=?",
                        (link["requested"], link["owner_id"], link["card"]),
                    )
            finally:
                db.close()


def install(app, connect):
    """개인 연결 상태·검토 목록만 제공하며 연결 ID·원문·인증 정보는 응답에 넣지 않습니다."""

    @app.get("/api/codef-sync")
    def codef_sync_data():
        db = connect()
        try:
            links = [
                dict(row)
                for row in db.execute(
                    "SELECT card,enabled,state,message,last_success,last_attempt,"
                    "(SELECT COUNT(*) FROM codef_events e WHERE e.owner_id=l.owner_id AND e.card=l.card AND e.state='saved') AS saved_count,"
                    "(SELECT COUNT(*) FROM codef_events e WHERE e.owner_id=l.owner_id AND e.card=l.card AND e.state='review') AS review_count "
                    "FROM codef_links l WHERE owner_id=? ORDER BY card",
                    (g.user["id"],),
                )
            ]
            # 직접 로그인으로 저장했어도 자동 수집 연결로 표시하지 않습니다.
            # 본인의 조회 시각·건수만 공개하고 인증 정보나 상대방 자료는 보내지 않습니다.
            manual = [
                dict(row)
                for row in db.execute(
                    "SELECT card,last_success,group_count,"
                    "(SELECT COUNT(*) FROM codef_events e WHERE e.owner_id=m.owner_id AND e.card=m.card AND e.state='saved') AS saved_count,"
                    "(SELECT COUNT(*) FROM codef_events e WHERE e.owner_id=m.owner_id AND e.card=m.card AND e.state='review') AS review_count "
                    "FROM codef_manual_runs m WHERE owner_id=? ORDER BY card",
                    (g.user["id"],),
                )
            ]
            items = [
                dict(row)
                for row in db.execute(
                    "SELECT id,card,used_on,merchant,net,kind,state,reason FROM codef_events WHERE owner_id=? AND state='review' ORDER BY used_on DESC,id DESC LIMIT 100",
                    (g.user["id"],),
                )
            ]
            heartbeat = db.execute("SELECT heartbeat FROM codef_worker_state WHERE id=1").fetchone()
            return jsonify(
                links=links,
                manual=manual,
                review=items,
                worker_running=bool(heartbeat and time.time() - heartbeat[0] < 600),
                schedule="한국시간 00:00·08:00·14:00·20:00",
            )
        finally:
            db.close()

    @app.post("/api/codef-sync/refresh")
    def codef_sync_refresh():
        db = connect()
        try:
            last = (
                db.execute(
                    "SELECT MAX(requested) FROM codef_links WHERE owner_id=?", (g.user["id"],)
                ).fetchone()[0]
                or 0
            )
            now = int(time.time())
            if now - last < 120:
                return jsonify(error="수동 요청은 2분 간격으로 가능합니다."), 429
            db.execute(
                "UPDATE codef_links SET requested=? WHERE owner_id=? AND enabled=1 AND state='ready'",
                (now, g.user["id"]),
            )
            g.audit_target = "본인 CODEF 수집 요청"
            return jsonify(ok=True)
        finally:
            db.close()

    @app.post("/api/codef-sync/pause")
    def codef_sync_pause():
        body = request.get_json(silent=True)
        card = body.get("card") if isinstance(body, dict) else None
        if not isinstance(card, str) or card not in CARD_OPTIONS:
            return jsonify(error="카드사를 확인해 주세요."), 400
        db = connect()
        try:
            db.execute(
                "UPDATE codef_links SET enabled=0,version=version+1 WHERE owner_id=? AND card=?",
                (g.user["id"], card),
            )
            g.audit_target = "본인 CODEF 수집 중지"
            return jsonify(ok=True)
        finally:
            db.close()

    @app.post("/api/codef-sync/review/<int:event_id>")
    def codef_sync_review(event_id):
        body = request.get_json(silent=True)
        action = body.get("action") if isinstance(body, dict) else None
        if action not in ("save", "ignore"):
            return jsonify(error="검토 동작을 확인해 주세요."), 400
        db = connect()
        try:
            row = db.execute(
                "SELECT * FROM codef_events WHERE owner_id=? AND id=? AND state='review'",
                (g.user["id"], event_id),
            ).fetchone()
            if not row:
                return jsonify(error="본인 검토 내역을 찾지 못했습니다."), 404
            if action == "save":
                if (
                    row["quarantined"]
                    or row["reason"] != "명세서와 중복 후보"
                    or row["kind"] != "0"
                    or row["net"] is None
                    or row["transaction_id"]
                ):
                    return (
                        jsonify(
                            error="취소·원본 상태를 명세서로 확인해 주세요. 자동 금액을 확정할 수 없습니다."
                        ),
                        400,
                    )
                transaction = insert_transaction(db, g.user["id"], row["card"], dict(row))
                db.execute(
                    "UPDATE codef_events SET state='saved',transaction_id=? WHERE id=?",
                    (transaction, event_id),
                )
            else:
                db.execute("UPDATE codef_events SET state='ignored' WHERE id=?", (event_id,))
            g.audit_target = "본인 CODEF 검토"
            return jsonify(ok=True)
        finally:
            db.close()
