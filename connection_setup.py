"""로그인한 본인이 휴대폰에서 카드·우리은행을 연결하는 일회성 인증 흐름입니다.

외부 요청은 DB 잠금 밖의 제한된 배경 스레드에서 수행합니다. 로그인 입력과 추가 인증
세션은 메모리에서만 사용하며, DB에는 재전송 방지용 작업 표식과 검증한 암호화 연결만
남깁니다. 현재 배포의 단일 웹 프로세스를 사용하고 재시작한 인증은 다시 전송하지 않습니다.
"""

from datetime import datetime, timedelta
import hashlib
import json
import re
import threading
import time
import uuid

from cryptography.fernet import Fernet
from flask import g, jsonify, request

import codef_bank as bank
import codef_card_auth as card_auth
import codef_sync as sync
from codef_card import (
    CARD_OPTIONS,
    CardCheckError,
    approval_payload,
    encrypt_password,
    query_approvals,
)
from codef_connection import ConnectionCheckError, configuration, config_digest

TTL = 600
AUTH_TTL = 170
MAX_RUNNING = 2
registry = {}
registry_lock = threading.RLock()


class SetupError(Exception):
    """인증 원문 없이 고정된 안내만 반환합니다."""


def schema(db):
    """비밀 없는 작업 표식으로 중복 실행·과도한 재인증을 차단합니다."""
    db.executescript("""CREATE TABLE IF NOT EXISTS connection_setups(
        id TEXT PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id),
        session_digest TEXT NOT NULL,nonce TEXT NOT NULL,kind TEXT NOT NULL,provider TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'running',created INTEGER NOT NULL,
        UNIQUE(owner_id,nonce));
        CREATE INDEX IF NOT EXISTS connection_setups_owner ON connection_setups(owner_id,created);
    """)


def session_digest():
    """세션 원문 대신 기존 로그인 쿠키의 해시만 작업에 결합합니다."""
    return hashlib.sha256(request.cookies.get("moa_session", "").encode()).hexdigest()


def login_text(value, label, strip=False):
    """strip이면 휴대폰 자동 완성·붙여넣기로 붙은 앞뒤 공백을 지웁니다(아이디 전용, 비밀번호는 그대로)."""
    if isinstance(value, str) and strip:
        value = value.strip()
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or not value.isprintable():
        raise SetupError(label + " 형식을 확인해 주세요.")
    return value


def prepare(payload, settings):
    """기관별 필수 입력을 먼저 검증하고 비밀번호·PIN을 즉시 RSA 암호화합니다."""
    allowed = {
        "nonce",
        "kind",
        "provider",
        "consent",
        "login_id",
        "password",
        "card_number",
        "pin",
        "user_name",
        "identity",
        "phone_no",
        "telecom",
        "account",
        "birth_date",
        "kb_card_number",
        "kb_pin",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) - allowed
        or payload.get("consent") is not True
    ):
        raise SetupError("본인 정보 전송·암호화 저장 안내에 동의한 뒤 연결해 주세요.")
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f-]{32,36}", nonce):
        raise SetupError("연결 화면을 다시 열어 주세요.")
    kind, provider = payload.get("kind"), payload.get("provider")
    login = login_text(payload.get("login_id"), "홈페이지 아이디", strip=True)
    password = login_text(payload.get("password"), "홈페이지 비밀번호")
    today = datetime.now(sync.KST).date()
    start, end = (today - timedelta(days=6)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
    if kind == "card" and isinstance(provider, str) and provider in CARD_OPTIONS:
        possession = kb_possession_input(provider, payload, settings)
        kwargs = {
            name: payload.get(name, "") for name in ("user_name", "identity", "phone_no", "telecom")
        }
        body = approval_payload(
            settings,
            provider,
            login,
            password,
            start,
            end,
            card_number=payload.get("card_number", ""),
            card_pin=payload.get("pin", ""),
            **kwargs,
        )
        entry = {
            key: body[key]
            for key in (
                "organization",
                "loginType",
                "id",
                "password",
                "userName",
                "identity",
                "phoneNo",
                "telecom",
            )
            if key in body
        }
        extra = {key: body[key] for key in ("cardNo", "loginCardNo", "cardPassword") if key in body}
        if provider == "hyundai":
            entry.update(cardNo=extra["cardNo"], cardPassword=extra["cardPassword"])
        entry.update(countryCode="KR", businessType="CD", clientType="P")
        return dict(
            nonce=nonce,
            kind=kind,
            provider=provider,
            entry=entry,
            extra=extra,
            possession=possession,
        )
    if kind != "bank" or provider != "woori":
        raise SetupError("연결할 카드사 또는 우리은행을 선택해 주세요.")
    account = payload.get("account", "")
    account = re.sub(r"[-\s]", "", account) if isinstance(account, str) else ""
    pin, birth = payload.get("pin"), payload.get("birth_date")
    if (
        not re.fullmatch(r"[0-9]{10,16}", account)
        or not isinstance(pin, str)
        or not re.fullmatch(r"[0-9]{4}", pin)
    ):
        raise SetupError("본인 계좌번호와 계좌 비밀번호 4자리 형식을 확인해 주세요.")
    try:
        if not isinstance(birth, str) or not re.fullmatch(r"[0-9]{8}", birth):
            raise ValueError()
        datetime.strptime(birth, "%Y%m%d")
    except ValueError:
        raise SetupError("생년월일은 YYYYMMDD 8자리로 입력해 주세요.") from None
    encrypted = encrypt_password(settings["public_key"], pin)
    entry = dict(
        countryCode="KR",
        businessType="BK",
        clientType="P",
        organization="0020",
        loginType="1",
        id=login,
        password=encrypt_password(settings["public_key"], password),
        **bank.woori_auth_fields(account, encrypted, birth),
    )
    return dict(
        nonce=nonce,
        kind=kind,
        provider=provider,
        entry=entry,
        extra=dict(account=account, accountPassword=encrypted, birthDate=birth),
    )


def kb_possession_input(provider, payload, settings):
    """KB 연결 화면에서 미리 받은 카드번호·비밀번호 앞 2자리를 검사하고 PIN을 바로 RSA 암호화합니다.

    KB는 하루 첫 조회에만 소지 확인(CF-12108)을 요구하고, 새로 등록한 연결의 첫 조회에서는
    요구하지 않을 때가 있습니다. 그때도 매일 자동 조회에 쓸 정보를 저장할 수 있도록 연결
    입력과 함께 받습니다. 값이 없으면 기존처럼 CF-12108을 받았을 때만 화면에서 묻습니다.
    """
    number, pin = payload.get("kb_card_number", ""), payload.get("kb_pin", "")
    if provider != "kb":
        if number or pin:
            raise SetupError("KB 이외의 카드에는 소지 확인 정보를 보내지 않습니다.")
        return {}
    if not number and not pin:
        return {}
    if not card_auth.valid_number(number) or not (
        isinstance(pin, str) and re.fullmatch(r"[0-9]{2}", pin)
    ):
        raise SetupError("KB 전체 카드번호와 카드 비밀번호 앞 2자리 형식을 확인해 주세요.")
    return dict(number=number, pin=encrypt_password(settings["public_key"], pin))


def previous_link(db, owner, prepared, key):
    """해당 사용자의 해당 기관·계좌만 선택합니다. 사용자 ID를 요청에서 받지 않습니다."""
    if prepared["kind"] == "card":
        row = db.execute(
            "SELECT * FROM codef_links WHERE owner_id=? AND card=?", (owner, prepared["provider"])
        ).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM bank_links WHERE owner_id=? AND account_tag=?",
            (owner, bank.account_tag(key, prepared["extra"]["account"])),
        ).fetchone()
    return dict(row) if row else None


class Job:
    """인증 연결 정보와 답변은 이 객체의 메모리 안에서만 잠시 유지합니다."""

    def __init__(self, record, revision, connect, settings, key, prepared, previous):
        self.record = record
        self.revision = revision
        self.connect = connect
        self.condition = threading.Condition(threading.RLock())
        self.state = "working"
        self.message = "기관에 연결하고 있습니다. 자동 재시도하지 않습니다."
        self.method = None
        self.challenge_id = None
        self.challenge_deadline = 0
        self.answer = None
        self.deadline = record["created"] + TTL
        self.thread = threading.Thread(
            target=self.run, args=(settings, key, prepared, previous), daemon=True
        )

    def snapshot(self):
        with self.condition:
            return dict(
                id=self.record["id"],
                kind=self.record["kind"],
                provider=self.record["provider"],
                state=self.state,
                message=self.message,
                method=self.method,
                challenge_id=self.challenge_id,
                expires_at=self.deadline,
            )

    def check(self, settings, key, prepared, previous, db=None):
        """요청 전후와 최종 쓰기 잠금 안에서 세션·버전·연결·키·취소를 다시 검사합니다."""
        with self.condition:
            if self.state in ("cancelled", "expired") or time.time() >= self.deadline:
                raise SetupError("인증이 취소되었거나 만료되어 새 연결을 저장하지 않았습니다.")
        own = db is None
        db = db or self.connect()
        try:
            valid = db.execute(
                "SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.digest=? AND s.user_id=? AND s.expires>? AND u.active=1",
                (self.record["session_digest"], self.record["owner_id"], int(time.time())),
            ).fetchone()
            version = db.execute(
                "SELECT revision FROM personal_revisions WHERE user_id=?",
                (self.record["owner_id"],),
            ).fetchone()
            active = db.execute(
                "SELECT state FROM connection_setups WHERE id=?", (self.record["id"],)
            ).fetchone()
            if not valid or not active or active[0] != "running":
                raise SetupError("로그인 또는 인증 작업이 종료되어 새 연결을 저장하지 않았습니다.")
            if (version[0] if version else 0) != self.revision or previous_link(
                db, self.record["owner_id"], prepared, key
            ) != previous:
                raise SetupError(
                    "인증 중 내 자료나 연결이 바뀌어 저장하지 않았습니다. 최신 화면에서 확인해 주세요."
                )
            sync.check_collection_configuration(key, config_digest(settings))
        finally:
            if own:
                db.close()

    def wait_answer(self, method, guard):
        """기관이 명시한 추가 인증만 기다리며 재전송은 사용자 답변 뒤 한 번씩 진행합니다."""
        sync.require_request_budget(self.connect, minimum=2 if method == "kb_possession" else 1)
        with self.condition:
            self.method = method
            self.challenge_id = uuid.uuid4().hex
            self.state = "needs_input"
            self.message = "추가 인증을 완료해 주세요."
            until = min(self.deadline, time.time() + AUTH_TTL)
            self.challenge_deadline = until
            try:
                while self.answer is None:
                    guard()
                    if time.time() >= until:
                        raise SetupError(
                            "추가 인증 시간이 지나 중단했습니다. 자동 재시도하지 않습니다."
                        )
                    self.condition.wait(timeout=min(2, until - time.time()))
                guard()
                if time.time() >= until:
                    raise SetupError("추가 인증 시간이 지나 중단했습니다.")
                value = self.answer
                self.answer = None
                self.state = "working"
                self.message = "추가 인증 결과를 확인하고 있습니다."
                return value
            finally:
                self.answer = None
                self.method = None
                self.challenge_id = None

    def run(self, settings, key, prepared, previous):
        """한 번의 명시적 연결만 처리하고, 성공 전에는 기존 연결·원장을 변경하지 않습니다."""
        owner = self.record["owner_id"]
        guard = lambda db=None: self.check(settings, key, prepared, previous, db)
        job = self

        class GuardedClient(sync.Client):
            def send(self, token, payload, operation="approval"):
                guard()
                result = super().send(token, payload, operation)
                guard()
                return result

        client = GuardedClient(self.connect)
        answer = lambda method: self.wait_answer(method, guard)
        try:
            guard()
            current, token = client.credentials()
            if config_digest(current) != config_digest(settings):
                raise SetupError("인증 중 서버 설정이 바뀌어 중단했습니다.")
            guard()
            today = datetime.now(sync.KST).date()
            start, end = (today - timedelta(days=29)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
            if prepared["kind"] == "bank":
                cid = bank.registration_id(
                    bank.query(
                        client, token, {"accountList": [prepared["entry"]]}, "register", answer
                    )
                )
                prepared["entry"].clear()
                fields = dict(prepared["extra"], connectedId=cid)
                data = bank.query(
                    client, token, bank.transaction_payload(fields, start, end), answer=answer
                )
                items = bank.normalize(data, fields["account"], start, end, key)
                with self.condition:

                    def final_guard(db):
                        guard(db)
                        db.execute(
                            "UPDATE connection_setups SET state='success' WHERE id=?",
                            (self.record["id"],),
                        )

                    bank.save_verified(
                        self.connect,
                        owner,
                        fields,
                        settings,
                        key,
                        items,
                        previous,
                        guard=final_guard,
                    )
                    self.state = "success"
                    self.message = "우리은행 연결을 완료했습니다. 계좌 입출금에서 확인해 주세요."
            else:
                provider = prepared["provider"]
                data = query_approvals(
                    token,
                    {"accountList": [prepared["entry"]]},
                    answer,
                    organization=CARD_OPTIONS[provider][0],
                    sender=lambda t, b: client.send(t, b, "register"),
                )
                cid = card_registration_id(data, provider)
                prepared["entry"].clear()
                body = dict(
                    organization=CARD_OPTIONS[provider][0],
                    connectedId=cid,
                    startDate=start,
                    endDate=end,
                    orderBy="0",
                    inquiryType="1",
                    memberStoreInfoType="0",
                )
                sealed = ""
                if provider == "hyundai":
                    extra = prepared["extra"]
                    card_auth.verify_reuse(
                        client, token, extra["cardNo"], extra["cardPassword"], cid, end, guard
                    )
                    sealed = card_auth.seal(
                        {"owner_id": owner, "card": "hyundai"},
                        cid,
                        settings,
                        key,
                        extra["cardNo"],
                        extra["cardPassword"],
                    )
                    restored, _ = card_auth.load(
                        {"owner_id": owner, "card": "hyundai", "card_auth_cipher": sealed},
                        cid,
                        settings,
                        key,
                    )
                    body.update(restored)
                # 연결 화면에서 미리 받은 KB 소지 정보입니다. 받았다면 저장을 고른 것으로 봅니다.
                early = prepared.get("possession") or {}

                def confirm_possession(number, pin_cipher, remember):
                    """소지 정보를 넣은 조회가 성공한 경우에만 같은 암호문을 이중 암호화합니다.

                    작업자는 하루 첫 조회의 CF-12108에만 이 값으로 한 번 다시 조회합니다.
                    """
                    temporary = dict(
                        body,
                        cardNo=number,
                        loginCardNo=number,
                        cardPassword=pin_cipher,
                    )
                    try:
                        query_approvals(token, temporary, answer, sender=client.send)
                    finally:
                        temporary.clear()
                    if not remember:
                        return ""
                    return card_auth.seal(
                        {"owner_id": owner, "card": "kb"}, cid, settings, key, number, pin_cipher
                    )

                try:
                    data = query_approvals(token, body, answer, sender=client.send)
                except CardCheckError as error:
                    if provider != "kb" or error.code != "CF-12108":
                        raise
                    if early:
                        sealed = confirm_possession(early["number"], early["pin"], True)
                    else:
                        values = answer("kb_possession")
                        try:
                            pin_cipher = encrypt_password(settings["public_key"], values.pop("pin"))
                            sealed = confirm_possession(
                                values["number"], pin_cipher, values.get("remember")
                            )
                        finally:
                            values.clear()
                    # 번호·PIN 없는 조회까지 성공해야 자동 연결입니다.
                    data = query_approvals(token, body, answer, sender=client.send)
                else:
                    if early:
                        # 새 연결의 첫 조회에서 KB가 소지 확인을 묻지 않은 경우입니다.
                        # 다음 날 00시 조회가 막히지 않도록 미리 받은 값으로 한 번 확인해 저장합니다.
                        sealed = confirm_possession(early["number"], early["pin"], True)
                items = sync.normalize(data, provider, start, end)
                with self.condition:
                    db = self.connect()
                    try:
                        with db:
                            db.execute("BEGIN IMMEDIATE")
                            guard(db)
                            now = int(time.time())
                            cipher = Fernet(key).encrypt(cid.encode()).decode()
                            db.execute(
                                "INSERT INTO codef_links(owner_id,card,cipher,app_digest,last_success,last_attempt,card_auth_cipher) VALUES(?,?,?,?,?,?,?) "
                                "ON CONFLICT(owner_id,card) DO UPDATE SET cipher=excluded.cipher,app_digest=excluded.app_digest,version=version+1,enabled=1,state='ready',message='',error_code='',last_success=excluded.last_success,last_attempt=excluded.last_attempt,card_auth_ref='',card_auth_cipher=excluded.card_auth_cipher",
                                (
                                    owner,
                                    provider,
                                    cipher,
                                    config_digest(settings),
                                    now,
                                    now,
                                    sealed,
                                ),
                            )
                            sync.store_events(db, owner, provider, items)
                            db.execute(
                                "INSERT OR IGNORE INTO codef_jobs(owner_id,card,slot,state,created) VALUES(?,?,?,'covered',?)",
                                (owner, provider, sync.latest_slot(), now),
                            )
                            sync.touch_revision(db, owner, "휴대폰 개인 카드 연결·조회 저장")
                            db.execute(
                                "UPDATE connection_setups SET state='success' WHERE id=?",
                                (self.record["id"],),
                            )
                    finally:
                        db.close()
                    self.state = "success"
                    self.message = "카드 연결을 완료했습니다. 사용 내역에서 확인해 주세요."
        except (
            SetupError,
            sync.SyncError,
            CardCheckError,
            card_auth.CardAuthError,
            ConnectionCheckError,
        ) as error:
            self.finish_error(str(error))
        except Exception:
            self.finish_error(
                "연결을 완료하지 못했습니다. 자동 재시도하지 않았으며 기존 자료를 유지합니다."
            )
        finally:
            prepared["entry"].clear()
            prepared["extra"].clear()
            prepared.get("possession", {}).clear()
            with self.condition:
                self.answer = None
                self.method = None
                self.challenge_id = None

    def finish_error(self, message):
        with self.condition:
            if self.state not in ("cancelled", "expired"):
                self.state = "error"
                self.message = message
            db = self.connect()
            try:
                with db:
                    db.execute(
                        "UPDATE connection_setups SET state=? WHERE id=? AND state='running'",
                        (self.state, self.record["id"]),
                    )
            finally:
                db.close()


def card_registration_id(data, provider):
    """단일 기관의 완전한 등록 성공만 허용하고 원문은 반환하지 않습니다."""
    success = data.get("successList") if isinstance(data, dict) else None
    if (
        not isinstance(success, list)
        or len(success) != 1
        or data.get("errorList")
        or not isinstance(success[0], dict)
        or any(
            success[0].get(k) != v
            for k, v in dict(
                organization=CARD_OPTIONS[provider][0],
                businessType="CD",
                clientType="P",
                code="CF-00000",
            ).items()
        )
    ):
        raise SetupError(
            "카드사 단일 계정 등록 성공을 확인하지 못했습니다. 기존 연결은 유지합니다."
        )
    cid = data.get("connectedId")
    if not isinstance(cid, str) or not 1 <= len(cid) <= 4096 or not cid.isprintable():
        raise SetupError("연결 ID 응답 형식을 확인하지 못했습니다.")
    return cid


def find_record(connect, identifier):
    """다른 사용자·다른 로그인 세션의 작업은 없는 작업과 같은 응답으로 처리합니다."""
    db = connect()
    try:
        row = db.execute(
            "SELECT * FROM connection_setups WHERE id=? AND owner_id=? AND session_digest=?",
            (identifier, g.user["id"], session_digest()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def public_status(connect, record):
    with registry_lock:
        job = registry.get(record["id"])
    if job is not None:
        return job.snapshot()
    if record["state"] == "running":
        db = connect()
        try:
            with db:
                db.execute(
                    "UPDATE connection_setups SET state='expired' WHERE id=? AND state='running'",
                    (record["id"],),
                )
        finally:
            db.close()
        record["state"] = "expired"
    return dict(
        id=record["id"],
        kind=record["kind"],
        provider=record["provider"],
        state=record["state"],
        message="인증 작업이 종료됐습니다. 새 연결은 자동 재시도하지 않습니다.",
        method=None,
        challenge_id=None,
        expires_at=record["created"] + TTL,
    )


def install(app, connect):
    """인증 API는 공통 세션·출처 검사를 유지하되 네트워크 동안 DB 잠금을 잡지 않습니다."""

    @app.get("/api/connections")
    def connection_options():
        configured = True
        try:
            configuration()
            sync.storage_key()
        except (ValueError, sync.SyncError):
            configured = False
        db = connect()
        try:
            version = db.execute(
                "SELECT revision FROM personal_revisions WHERE user_id=?", (g.user["id"],)
            ).fetchone()
            recent = db.execute(
                "SELECT * FROM connection_setups WHERE owner_id=? AND session_digest=? ORDER BY created DESC,rowid DESC LIMIT 1",
                (g.user["id"], session_digest()),
            ).fetchone()
            response = jsonify(
                configured=configured,
                cards=[dict(key=k, name=v[1]) for k, v in CARD_OPTIONS.items()],
                banks=[dict(key="woori", name="우리은행")],
                current=public_status(connect, dict(recent)) if recent else None,
            )
            response.headers["X-Moa-Revision"] = str(version[0] if version else 0)
            return response
        finally:
            db.close()

    @app.post("/api/connections/setup")
    def connection_start():
        if request.content_length is None or request.content_length > 16384:
            return jsonify(error="연결 입력 크기를 확인해 주세요."), 400
        payload = request.get_json(silent=True)
        prepared = None
        try:
            settings = configuration()
            key = sync.storage_key()
            prepared = prepare(payload, settings)
            # 현대는 재사용 검증 두 번, KB 소지 정보는 확인 조회 한 번(+재조회)이 더 듭니다.
            minimum = (
                5
                if prepared["kind"] == "card" and prepared["provider"] == "hyundai"
                else 4 if prepared.get("possession") else 3
            )
            with registry_lock:
                db = connect()
                try:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        previous_record = db.execute(
                            "SELECT * FROM connection_setups WHERE owner_id=? AND nonce=?",
                            (g.user["id"], prepared["nonce"]),
                        ).fetchone()
                        if previous_record:
                            if (
                                previous_record["session_digest"] != session_digest()
                                or previous_record["kind"] != prepared["kind"]
                                or previous_record["provider"] != prepared["provider"]
                            ):
                                return (
                                    jsonify(
                                        error="이 연결 요청은 종료됐습니다. 화면을 다시 열어 주세요."
                                    ),
                                    409,
                                )
                            db.rollback()
                            return jsonify(public_status(connect, dict(previous_record))), 200
                        version = db.execute(
                            "SELECT revision FROM personal_revisions WHERE user_id=?",
                            (g.user["id"],),
                        ).fetchone()
                        revision = version[0] if version else 0
                        if request.headers.get("If-Match") != str(revision):
                            return (
                                jsonify(
                                    error="최신 연결 화면을 다시 연 뒤 인증해 주세요.",
                                    code="revision_conflict",
                                ),
                                409,
                            )
                        now = int(time.time())
                        db.execute(
                            "UPDATE connection_setups SET state='expired' WHERE state='running' AND created<?",
                            (now - TTL,),
                        )
                        active = db.execute(
                            "SELECT 1 FROM connection_setups WHERE owner_id=? AND state='running'",
                            (g.user["id"],),
                        ).fetchone()
                        count = db.execute(
                            "SELECT count(*) FROM connection_setups WHERE owner_id=? AND kind=? AND provider=? AND created>?",
                            (g.user["id"], prepared["kind"], prepared["provider"], now - 300),
                        ).fetchone()[0]
                        recent = db.execute(
                            "SELECT 1 FROM connection_setups WHERE owner_id=? AND kind=? AND provider=? AND created>?",
                            (g.user["id"], prepared["kind"], prepared["provider"], now - 60),
                        ).fetchone()
                        if (
                            active
                            or count >= 3
                            or recent
                            or sum(j.thread.is_alive() for j in registry.values()) >= MAX_RUNNING
                        ):
                            return (
                                jsonify(
                                    error="진행 중인 인증을 마치거나 1분 뒤 다시 시도해 주세요. 반복 요청하지 않습니다."
                                ),
                                429,
                            )
                        # 같은 쓰기 연결로 예산을 읽어 추가 연결·스키마 잠금 충돌을 피합니다.
                        day = datetime.now(sync.KST).strftime("%Y-%m-%d")
                        used = db.execute(
                            "SELECT requests FROM codef_budget WHERE day=?", (day,)
                        ).fetchone()
                        if (used[0] if used else 0) + minimum > sync.daily_request_limit():
                            return (
                                jsonify(
                                    error="오늘의 연결 요청 예산이 부족합니다. 입력은 저장하지 않았습니다."
                                ),
                                429,
                            )
                        record = dict(
                            id=uuid.uuid4().hex,
                            owner_id=g.user["id"],
                            session_digest=session_digest(),
                            nonce=prepared["nonce"],
                            kind=prepared["kind"],
                            provider=prepared["provider"],
                            state="running",
                            created=now,
                        )
                        previous = previous_link(db, g.user["id"], prepared, key)
                        db.execute(
                            "INSERT INTO connection_setups(id,owner_id,session_digest,nonce,kind,provider,created) VALUES(:id,:owner_id,:session_digest,:nonce,:kind,:provider,:created)",
                            record,
                        )
                    # 실제 외부 호출은 이 짧은 저장 단위가 끝난 뒤 각각 예산을 차감합니다.
                    job = Job(record, revision, connect, settings, key, prepared, previous)
                    for identifier, old in list(registry.items()):
                        if not old.thread.is_alive() and old.deadline < time.time():
                            registry.pop(identifier, None)
                    registry[record["id"]] = job
                    job.thread.start()
                    prepared = None
                    return jsonify(job.snapshot()), 202
                finally:
                    db.close()
        except (SetupError, CardCheckError, sync.SyncError, ValueError) as error:
            return jsonify(error=str(error)), 400
        finally:
            if isinstance(payload, dict):
                payload.clear()
            if prepared is not None:
                prepared["entry"].clear()
                prepared["extra"].clear()
                prepared.get("possession", {}).clear()

    @app.get("/api/connections/setup/<identifier>")
    def connection_status(identifier):
        record = find_record(connect, identifier)
        if not record:
            return jsonify(error="인증 작업을 찾지 못했습니다."), 404
        return jsonify(public_status(connect, record))

    @app.post("/api/connections/setup/<identifier>/answer")
    def connection_answer(identifier):
        record = find_record(connect, identifier)
        if not record:
            return jsonify(error="인증 작업을 찾지 못했습니다."), 404
        payload = request.get_json(silent=True)
        with registry_lock:
            job = registry.get(identifier)
        if job is None:
            return jsonify(error="인증이 만료됐습니다. 자동 재시도하지 않습니다."), 409
        with job.condition:
            if (
                job.state != "needs_input"
                or time.time() >= min(job.deadline, job.challenge_deadline)
                or not isinstance(payload, dict)
                or payload.get("challenge_id") != job.challenge_id
                or job.answer is not None
            ):
                return jsonify(error="유효한 추가 인증 요청이 없습니다."), 409
            value = payload.get("answer")
            if job.method == "kb_possession":
                valid = (
                    isinstance(value, dict)
                    and set(value) in ({"number", "pin"}, {"number", "pin", "remember"})
                    and card_auth.valid_number(value.get("number"))
                    and isinstance(value.get("pin"), str)
                    and re.fullmatch(r"[0-9]{2}", value["pin"])
                    and type(value.get("remember", False)) is bool
                )
            else:
                pattern = r"[0-9]{1,32}" if job.method == "smsAuthNo" else r"[A-Za-z0-9]{1,32}"
                valid = (
                    isinstance(value, str)
                    and re.fullmatch(pattern, value)
                    and (job.method != "simpleAuth" or value == "1")
                )
            if not valid:
                return jsonify(error="추가 인증 입력 형식을 확인해 주세요."), 400
            job.answer = value
            job.state = "working"
            job.condition.notify_all()
        return jsonify(ok=True)

    @app.post("/api/connections/setup/<identifier>/cancel")
    def connection_cancel(identifier):
        record = find_record(connect, identifier)
        if not record:
            return jsonify(error="인증 작업을 찾지 못했습니다."), 404
        with registry_lock:
            job = registry.get(identifier)
        if job is not None:
            with job.condition:
                if job.state in ("working", "needs_input"):
                    job.state = "cancelled"
                    job.message = (
                        "인증을 취소했습니다. 이미 전송 중인 요청은 돌아와도 저장하지 않습니다."
                    )
                    job.answer = None
                    db = connect()
                    try:
                        with db:
                            db.execute(
                                "UPDATE connection_setups SET state='cancelled' WHERE id=? AND state='running'",
                                (identifier,),
                            )
                    finally:
                        db.close()
                    job.condition.notify_all()
                return jsonify(job.snapshot())
        return jsonify(public_status(connect, record))
