"""로그인과 세션 관리: 소유자와 공동관리자를 식별하고 인증되지 않은 접근을 막습니다.

비밀번호 원문 대신 해시를 저장합니다. 브라우저 쿠키의 임의 토큰도 DB에는 해시로 저장하며,
요청마다 만료 시각·계정 활성 여부를 검사합니다.
'로그인 유지'(sessions.remember=1)는 90일 세션이며, 하루에 한 번 이상 쓰면 다시 90일로 늘어납니다.
로그아웃·비밀번호 변경·계정 해제는 이 세션도 바로 끝냅니다. 이 파일의 인증 검사가 공동 관리 검사보다 먼저 실행됩니다.
"""

import hashlib
import secrets
import re
import sqlite3
import time
from urllib.parse import urlsplit

from flask import jsonify, redirect, render_template, request, g
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

COOKIE = "moa_session"
LIFETIME = 8 * 60 * 60
REMEMBER_LIFETIME = 90 * 24 * 60 * 60
RENEW_AFTER = 24 * 60 * 60


# 1. 계정 저장 구조와 기존 개인 계정 이전
# ─────────────────────────────────────────────────────────────────
def auth_schema(db):
    """계정·세션 테이블을 준비하고 기존 개인용 계정과 세션을 소유자 계정으로 연결합니다."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS owner (id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS login_failures (at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,
            role TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS workspace_revision(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL);
        INSERT OR IGNORE INTO workspace_revision VALUES(1,0);
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY,actor_id INTEGER NOT NULL,actor_name TEXT NOT NULL,
            action TEXT NOT NULL,target TEXT NOT NULL,created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        INSERT OR IGNORE INTO users(id,username,password_hash,role) SELECT 1,'owner',password_hash,'owner' FROM owner WHERE id=1;
    """)
    if "user_id" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
        try:
            db.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            if "user_id" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
                raise
        db.commit()


# 2. 터미널에서 소유자 비밀번호 설정
# ─────────────────────────────────────────────────────────────────
def set_password(connect, password):
    """터미널에서 소유자 비밀번호 해시를 갱신하고 모든 기존 세션을 종료합니다."""
    if len(password) < 12 or len(password) > 128:
        raise ValueError("비밀번호는 12~128자로 설정해 주세요.")
    hashed = generate_password_hash(password)
    db = connect()
    try:
        with db:
            db.execute(
                (
                    "INSERT INTO owner(id,password_hash) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET "
                    "password_hash=excluded.password_hash"
                ),
                (hashed,),
            )
            db.execute(
                (
                    "INSERT INTO users(id,username,password_hash,role) VALUES(1,'owner',?,'owner') ON "
                    "CONFLICT(id) DO UPDATE SET password_hash=excluded.password_hash,active=1"
                ),
                (hashed,),
            )
            # 터미널 암호 초기화도 소유자의 이전 단축어 키를 함께 폐기합니다.
            db.execute("DELETE FROM sms_keys WHERE user_id=1")
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM login_failures")
    finally:
        db.close()


# 3. 요청 검사와 로그인 API 등록
# ─────────────────────────────────────────────────────────────────
def install_auth(app, connect):
    """로그인 검사와 로그인·로그아웃 API를 Flask 앱에 등록합니다."""
    origin = app.config.get("PUBLIC_ORIGIN", "")
    if origin:
        url = urlsplit(origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.path
            or url.query
            or url.fragment
            or url.username
        ):
            raise ValueError("MOA_PUBLIC_ORIGIN은 경로 없는 HTTPS 주소여야 합니다.")
    if app.config.get("TRUST_PROXY"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0)

    def authenticated(db):
        """쿠키 토큰의 해시로 활성 세션을 찾고, 요청 전용 공간 g에 사용자 정보를 넣습니다."""
        raw = request.cookies.get(COOKIE, "")
        if not raw or len(raw) > 128:
            return False
        digest = hashlib.sha256(raw.encode()).hexdigest()
        now = int(time.time())
        row = db.execute(
            (
                "SELECT u.id,u.username,u.role,s.remember,s.expires FROM sessions s "
                "JOIN users u ON u.id=s.user_id WHERE s.digest=? AND s.expires>? AND u.active=1"
            ),
            (digest, now),
        ).fetchone()
        if row:
            g.user = {key: row[key] for key in ("id", "username", "role")}
            # 로그인 유지 세션은 쓸 때마다(하루 한 번) 만료를 다시 90일로 늘리고 쿠키도 새로 줍니다.
            if row["remember"] and row["expires"] - now < REMEMBER_LIFETIME - RENEW_AFTER:
                db.execute(
                    "UPDATE sessions SET expires=? WHERE digest=?",
                    (now + REMEMBER_LIFETIME, digest),
                )
                db.commit()
                g.renew_session = raw
        return row is not None

    def session_cookie(response, token, remember):
        response.set_cookie(
            COOKIE,
            token,
            max_age=REMEMBER_LIFETIME if remember else LIFETIME,
            httponly=True,
            secure=bool(app.config.get("PUBLIC_ORIGIN")),
            samesite="Strict",
            path="/",
        )

    @app.before_request
    def protect():
        """주소·HTTPS·위조 요청 방지 헤더·로그인을 검사한 뒤에만 실제 API로 넘깁니다."""
        if request.endpoint == "health":
            return None
        expected = app.config.get("PUBLIC_ORIGIN", "")
        if expected:
            if request.host != urlsplit(expected).netloc or not request.is_secure:
                return jsonify(error="설정된 HTTPS 주소로 접속해 주세요."), 400
        elif request.host.split(":")[0] not in ("localhost", "127.0.0.1", "[::1]"):
            return jsonify(error="외부 접속에는 HTTPS 주소 설정이 필요합니다."), 400
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if request.headers.get("X-Requested-With") != "CardDashboard":
                return jsonify(error="앱 화면에서 다시 요청해 주세요."), 403
            supplied = request.headers.get("Origin")
            if supplied and supplied != (expected or request.host_url.rstrip("/")):
                return jsonify(error="다른 사이트에서 보낸 요청은 허용하지 않습니다."), 403
        if request.endpoint in (
            "sms_receive",  # 세션 대신 전용 쓰기 키를 수신 API 내부에서 검증합니다.
            "login",
            "login_submit",
            "session_status",
            "static",
            "pwa_service_worker",
        ):
            return None
        db = connect()
        try:
            allowed = authenticated(db)
        finally:
            db.close()
        if not allowed:
            if request.path.startswith("/api/"):
                return jsonify(error="로그인이 필요합니다."), 401
            return redirect("/login")
        # 다른 탭에서 로그인 계정을 바꾼 뒤 이전 폼을 저장하는 것도 차단합니다.
        # 이 헤더는 계정을 지정하는 권한이 아니라 현재 세션과 화면이 일치하는지 확인하는 표식입니다.
        page_user = request.headers.get("X-Moa-User")
        if page_user is not None and page_user != str(g.user["id"]):
            return (
                jsonify(
                    error="로그인 계정이 바뀌었습니다. 최신 화면을 다시 열어 주세요.",
                    code="account_changed",
                ),
                409,
            )

    @app.after_request
    def security_headers(response):
        """캐시·스크립트 실행·외부 프레임 포함을 제한하는 보안 응답 헤더를 붙입니다."""
        renewed = g.pop("renew_session", None)
        if renewed:
            session_cookie(response, renewed, True)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if app.config.get("PUBLIC_ORIGIN"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/healthz")
    def health():
        return jsonify(ok=True)

    @app.get("/login")
    def login():
        return render_template("login.html")

    @app.get("/api/session")
    def session_status():
        """비밀번호 초기 설정 여부와 현재 로그인 상태만 반환합니다. 비밀번호 해시는 보내지 않습니다."""
        db = connect()
        try:
            allowed = authenticated(db)
            return jsonify(
                configured=db.execute("SELECT 1 FROM owner").fetchone() is not None,
                authenticated=allowed,
                user=g.user if allowed else None,
            )
        finally:
            db.close()

    @app.post("/api/login")
    def login_submit():
        """계정과 비밀번호를 확인하고 세션을 발급합니다(기본 8시간, '로그인 유지'면 90일).
        연속 실패 횟수도 함께 검사합니다."""
        payload = request.get_json(silent=True) or {}
        password = payload.get("password", "") if isinstance(payload, dict) else ""
        if not isinstance(password, str) or len(password) > 128:
            return jsonify(error="비밀번호를 확인해 주세요."), 400
        username = payload.get("username", "owner") if isinstance(payload, dict) else "owner"
        if not isinstance(username, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,39}", username):
            return jsonify(error="아이디와 비밀번호를 확인해 주세요."), 401
        remember = isinstance(payload, dict) and payload.get("remember") is True
        db = connect()
        try:
            now = int(time.time())
            # 동시 로그인 요청도 실패 횟수 제한을 우회하지 못하도록 검사와 증가를 직렬화합니다.
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_failures WHERE at<?", (now - 300,))
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            owner = db.execute("SELECT password_hash FROM owner WHERE id=1").fetchone()
            if not owner:
                db.commit()
                return (
                    jsonify(
                        error="아직 로그인 비밀번호가 설정되지 않았습니다. 서버에서 계정을 초기화해 주세요."
                    ),
                    503,
                )
            if db.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0] >= 5:
                db.commit()
                response = jsonify(error="로그인 시도가 많습니다. 5분 뒤 다시 시도해 주세요.")
                response.headers["Retry-After"] = "300"
                return response, 429
            user = db.execute(
                "SELECT * FROM users WHERE username=? AND active=1", (username,)
            ).fetchone()
            # 없는 아이디도 해시 검증을 실행해 응답 시간만으로 계정 존재를 추측하기 어렵게 합니다.
            valid = check_password_hash(
                user["password_hash"] if user else owner["password_hash"], password
            )
            if not user or not valid:
                db.execute("INSERT INTO login_failures(at) VALUES(?)", (now,))
                db.commit()
                return jsonify(error="비밀번호가 맞지 않습니다."), 401
            old = request.cookies.get(COOKIE, "")
            db.execute(
                "DELETE FROM sessions WHERE digest=?", (hashlib.sha256(old.encode()).hexdigest(),)
            )
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO sessions(digest,expires,user_id,remember) VALUES(?,?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    now + (REMEMBER_LIFETIME if remember else LIFETIME),
                    user["id"],
                    int(remember),
                ),
            )
            db.execute("DELETE FROM login_failures")
            db.commit()
            response = jsonify(ok=True)
            session_cookie(response, token, remember)
            return response
        finally:
            password = ""
            db.close()

    @app.post("/api/logout")
    def logout():
        """현재 브라우저의 세션을 서버와 쿠키에서 모두 폐기합니다."""
        g.pop("renew_session", None)
        raw = request.cookies.get(COOKIE, "")
        db = connect()
        try:
            with db:
                db.execute(
                    "DELETE FROM sessions WHERE digest=?",
                    (hashlib.sha256(raw.encode()).hexdigest(),),
                )
        finally:
            db.close()
        response = jsonify(ok=True)
        response.delete_cookie(
            COOKIE, secure=bool(app.config.get("PUBLIC_ORIGIN")), httponly=True, samesite="Strict"
        )
        return response
