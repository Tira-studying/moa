"""CODEF 데모 키 설정과 개발자 토큰 발급 확인을 담당합니다.

웹 기능은 개발자 인증 확인입니다. 카드 계정 등록·승인 조회·예약 수집은 여기서 실행하지 않습니다.
키는 읽기 전용 비밀 파일에서 읽고, 토큰·키 원문은 응답·DB·로그에 보관하지 않습니다.
인증 확인만 소유자가 수동 요청할 수 있고, 짧은 DB 저장 단위 사이에서 네트워크를 사용합니다.
"""

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import g, jsonify, request

TOKEN_URL = "https://oauth.codef.io/oauth/token"
DEMO_HOST = "https://development.codef.io"
CONFIG_LIMIT = 32768
RESPONSE_LIMIT = 32768
COOLDOWN = 60


class ConnectionCheckError(Exception):
    """공급자 응답이나 예외 원문 대신 화면에 안전한 고정 문구만 전달합니다."""


def validate_credential(name, value):
    """입력 항목을 고정된 이름으로 구분합니다. 잘못된 값 자체는 오류 문구에 포함하지 않습니다."""
    labels = {"client_id": "데모 client_id", "client_secret": "데모 client_secret"}
    label = labels[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}가 비어 있습니다. 해당 항목의 복사 버튼으로 복사해 주세요.")
    value = value.strip()
    if (
        len(value) > 512
        or not value.isascii()
        or not value.isprintable()
        or any(character.isspace() for character in value)
        or (name == "client_id" and ":" in value)
    ):
        raise ValueError(f"{label} 형식을 확인해 주세요. 항목 이름 대신 복사한 값만 넣어 주세요.")
    return value


def validate_public_key(public):
    """CODEF 제공 RSA 공개키를 읽습니다. 서버의 최소 키 길이를 임의로 1024비트로 가정하지 않습니다."""
    if not isinstance(public, str) or not 1 <= len(public.strip()) <= 16384:
        raise ValueError("public_key가 비어 있거나 너무 깁니다. 공개키 복사 버튼을 사용해 주세요.")
    public = public.strip()
    try:
        if public.startswith(("-----BEGIN PUBLIC KEY-----", "-----BEGIN RSA PUBLIC KEY-----")):
            key = serialization.load_pem_public_key(public.encode("ascii"))
        else:
            key = serialization.load_der_public_key(
                base64.b64decode("".join(public.split()), validate=True)
            )
        # 공급자가 제공한 공개키를 형식 검증할 뿐, 로컬에서 새 키를 발급하지 않습니다.
        if not isinstance(key, rsa.RSAPublicKey) or not 512 <= key.key_size <= 8192:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, UnsupportedAlgorithm):
        raise ValueError(
            "public_key를 RSA 공개키로 읽지 못했습니다. 복사 버튼으로 전체 값을 다시 복사해 주세요."
        ) from None
    return public


def validate_settings(value):
    """데모 설정을 확인하고 키 값이 없는 안전한 항목별 오류를 반환합니다."""
    if not isinstance(value, dict) or value.get("environment") != "demo":
        raise ValueError("데모 환경의 CODEF 설정을 확인해 주세요.")
    return dict(
        environment="demo",
        client_id=validate_credential("client_id", value.get("client_id")),
        client_secret=validate_credential("client_secret", value.get("client_secret")),
        public_key=validate_public_key(value.get("public_key")),
    )


def configuration():
    """비밀 파일은 한 번에 제한된 크기만 읽습니다. 경로·내용이 들어간 OS 오류는 공개하지 않습니다."""
    path = Path(os.environ.get("MOA_CODEF_CONFIG_FILE", "/run/moa-secrets/codef-app.json"))
    try:
        with path.open("rb") as stream:
            data = stream.read(CONFIG_LIMIT + 1)
        if len(data) > CONFIG_LIMIT:
            raise ValueError()
        return validate_settings(json.loads(data))
    except (OSError, ValueError, UnicodeError):
        raise ValueError("서버의 CODEF 데모 키 설정을 확인해 주세요.") from None


def config_digest(settings):
    """설정이 교체된 뒤 예전 인증 성공 결과를 표시하지 않도록 내부 해시만 비교합니다."""
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def schema(db):
    """개발자 인증 결과와 재요청 시각만 저장합니다. 자격 증명이나 카드 데이터는 없습니다."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS codef_app_check("
        "id INTEGER PRIMARY KEY CHECK(id=1),digest TEXT NOT NULL,attempt TEXT NOT NULL,"
        "attempted_at INTEGER NOT NULL,next_allowed INTEGER NOT NULL,checked_at INTEGER,"
        "ok INTEGER,message TEXT NOT NULL DEFAULT '')"
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """인증 헤더가 다른 주소로 전달되지 않도록 OAuth 리다이렉트를 허용하지 않습니다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def issue_token(settings):
    """고정 HTTPS 서버에서 토큰을 받습니다. 호출자는 메모리에서만 사용해야 합니다."""
    authorization = base64.b64encode(
        (settings["client_id"] + ":" + settings["client_secret"]).encode()
    ).decode("ascii")
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "read"}).encode()
    outgoing = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Basic " + authorization,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect()
    )
    try:
        with opener.open(outgoing, timeout=10) as response:
            if response.status != 200:
                raise ConnectionCheckError("CODEF 인증 서버 응답을 확인하지 못했습니다.")
            content = response.read(RESPONSE_LIMIT + 1)
        if len(content) > RESPONSE_LIMIT:
            raise ValueError()
        value = json.loads(content)
        token = value.get("access_token") if isinstance(value, dict) else None
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 8192
            or not token.isascii()
            or not token.isprintable()
            or any(c.isspace() for c in token)
        ):
            raise ValueError()
        token_type = value.get("token_type", "Bearer")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise ValueError()
        return token
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if status in (400, 401, 403):
            raise ConnectionCheckError("Client ID·Secret과 데모 키 선택을 확인해 주세요.") from None
        if status == 429:
            raise ConnectionCheckError(
                "CODEF에서 요청을 제한했습니다. 잠시 뒤 다시 확인해 주세요."
            ) from None
        raise ConnectionCheckError("CODEF 인증 서버가 요청을 처리하지 못했습니다.") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise ConnectionCheckError(
            "CODEF에 연결하지 못했습니다. 서버의 인터넷·인증서 설정을 확인해 주세요."
        ) from None
    except (ValueError, TypeError, UnicodeError):
        raise ConnectionCheckError("CODEF 토큰 발급 응답 형식을 확인하지 못했습니다.") from None


def check_developer_token(settings):
    """웹 화면의 인증 시험에서는 토큰을 반환하거나 저장하지 않습니다."""
    issue_token(settings)


def owner_session(db):
    """네트워크 호출 전후에 세션과 소유자 권한을 다시 확인합니다. 오래된 요청은 결과를 저장하지 않습니다."""
    digest = hashlib.sha256(request.cookies.get("moa_session", "").encode()).hexdigest()
    return (
        db.execute(
            "SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.digest=? AND s.expires>? AND u.id=? AND u.active=1 AND u.role='owner'",
            (digest, int(time.time()), g.user["id"]),
        ).fetchone()
        is not None
    )


def status(db, settings):
    """설정 여부·고정 환경·인증 결과만 반환하며 식별자·토큰·공개키도 보내지 않습니다."""
    row = db.execute("SELECT * FROM codef_app_check WHERE id=1").fetchone()
    result = dict(
        configured=True,
        environment="demo",
        api_host=DEMO_HOST,
        checked_at=None,
        ok=None,
        message="데모 키 설정을 읽었습니다. 개발자 인증 확인을 실행해 주세요.",
    )
    if row and row["digest"] == config_digest(settings):
        result.update(
            checked_at=row["checked_at"], ok=None if row["ok"] is None else bool(row["ok"])
        )
        if row["checked_at"]:
            result["message"] = row["message"]
        elif int(time.time()) - row["attempted_at"] < 15:
            result["message"] = "개발자 인증을 확인하고 있습니다."
        else:
            result["message"] = "이전 확인을 완료하지 못했습니다. 다시 확인해 주세요."
    return result


def install(app, connect):
    """앱 소유자에게만 서버 설정 상태와 수동 토큰 확인을 제공합니다."""

    @app.get("/api/codef/status")
    def codef_status():
        if g.user["role"] != "owner":
            return jsonify(error="소유자만 CODEF 앱 설정을 확인할 수 있습니다."), 403
        db = connect()
        try:
            try:
                settings = configuration()
            except ValueError as error:
                return jsonify(configured=False, environment="demo", ok=None, message=str(error))
            return jsonify(status(db, settings))
        finally:
            db.close()

    @app.post("/api/codef/check")
    def codef_check():
        if g.user["role"] != "owner":
            return jsonify(error="소유자만 CODEF 개발자 인증을 확인할 수 있습니다."), 403
        try:
            settings = configuration()
        except ValueError as error:
            return jsonify(error=str(error)), 400
        now = int(time.time())
        digest = config_digest(settings)
        attempt = uuid.uuid4().hex
        db = connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            if not owner_session(db):
                return jsonify(error="로그인 상태를 다시 확인해 주세요."), 401
            old = db.execute("SELECT * FROM codef_app_check WHERE id=1").fetchone()
            if old and old["digest"] == digest and old["next_allowed"] > now:
                response = jsonify(
                    error="인증 확인은 1분에 한 번 가능합니다. 잠시 뒤 다시 눌러 주세요."
                )
                response.headers["Retry-After"] = str(old["next_allowed"] - now)
                return response, 429
            db.execute(
                "INSERT INTO codef_app_check(id,digest,attempt,attempted_at,next_allowed) VALUES(1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET digest=excluded.digest,attempt=excluded.attempt,"
                "attempted_at=excluded.attempted_at,next_allowed=excluded.next_allowed,checked_at=NULL,ok=NULL,message=''",
                (digest, attempt, now, now + COOLDOWN),
            )
            db.commit()
        finally:
            db.rollback()
            db.close()
        # 긴 네트워크 작업 중 개인 가계부 DB 쓰기 잠금을 유지하지 않습니다.
        ok = True
        message = "CODEF 개발자 토큰 발급에 성공했습니다. 카드사 연결·데모 승인내역 조회 권한은 다음 단계에서 확인합니다."
        try:
            check_developer_token(settings)
        except ConnectionCheckError as error:
            ok = False
            message = str(error)
        db = connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            if not owner_session(db):
                return jsonify(error="로그인 상태가 바뀌었습니다. 다시 로그인해 주세요."), 401
            updated = db.execute(
                "UPDATE codef_app_check SET checked_at=?,ok=?,message=? WHERE id=1 AND digest=? AND attempt=?",
                (int(time.time()), int(ok), message, digest, attempt),
            )
            if not updated.rowcount:
                return (
                    jsonify(error="설정 확인 요청이 바뀌었습니다. 상태를 다시 불러와 주세요."),
                    409,
                )
            db.execute(
                "INSERT INTO audit_log(actor_id,actor_name,action,target,data_owner_id) VALUES(?,?,?,?,?)",
                (
                    g.user["id"],
                    g.user["username"],
                    "CODEF 개발자 인증 확인",
                    "데모 앱 설정",
                    g.user["id"],
                ),
            )
            result = status(db, settings)
            db.commit()
            return jsonify(result)
        finally:
            db.rollback()
            db.close()
