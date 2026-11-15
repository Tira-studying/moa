"""검증 범위: 로그인·세션 만료·위조 요청 차단.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app, connect
from auth import COOKIE, set_password

HEADERS = {"X-Requested-With": "CardDashboard"}
PASSWORD = "fixture-login-password"


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, DATABASE=str(Path(self.tmp.name) / "auth.sqlite3"), PUBLIC_ORIGIN=""
        )
        self.client = app.test_client()

    def tearDown(self):
        app.config["PUBLIC_ORIGIN"] = ""
        self.tmp.cleanup()

    def login(self, password=PASSWORD, **kwargs):
        return self.client.post(
            "/api/login", json={"password": password}, headers=HEADERS, **kwargs
        )

    def test_unconfigured_is_closed(self):
        self.assertEqual(self.client.get("/api/data").status_code, 401)
        self.assertEqual(self.client.get("/").location, "/login")
        self.assertEqual(self.login().status_code, 503)
        self.assertEqual(self.client.get("/healthz").json, {"ok": True})
        self.assertEqual(self.client.post("/api/secure-html", headers=HEADERS).status_code, 401)
        self.assertEqual(self.client.get("/sample.csv").status_code, 302)

    def test_login_logout_and_cookie_replay(self):
        set_password(connect, PASSWORD)
        response = self.login()
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        raw = self.client.get_cookie(COOKIE).value
        db = connect()
        try:
            self.assertNotEqual(
                db.execute("SELECT password_hash FROM owner").fetchone()[0], PASSWORD
            )
            self.assertEqual(
                db.execute("SELECT digest FROM sessions").fetchone()[0],
                hashlib.sha256(raw.encode()).hexdigest(),
            )
        finally:
            db.close()
        self.assertEqual(self.client.get("/api/data").status_code, 200)
        self.assertEqual(self.client.post("/api/logout", headers=HEADERS).status_code, 200)
        self.client.set_cookie(COOKIE, raw)
        self.assertEqual(self.client.get("/api/data").status_code, 401)

    def test_wrong_password_rate_limit_and_recovery(self):
        set_password(connect, PASSWORD)
        for _ in range(5):
            self.assertEqual(self.login("wrong").status_code, 401)
        self.assertEqual(self.login().status_code, 429)
        with patch("auth.time.time", return_value=time.time() + 301):
            self.assertEqual(self.login().status_code, 200)

    def test_rotation_expiry_and_tampering(self):
        set_password(connect, PASSWORD)
        self.login()
        with patch("auth.time.time", return_value=time.time() + 8 * 3600 + 1):
            self.assertEqual(self.client.get("/api/data").status_code, 401)
        set_password(connect, "new-fixture-password")
        self.assertEqual(self.client.get("/api/data").status_code, 401)
        self.client.set_cookie(COOKIE, "tampered")
        self.assertEqual(self.client.get("/api/data").status_code, 401)
        self.assertEqual(self.login(PASSWORD).status_code, 401)

    def session_row(self):
        db = connect()
        try:
            return db.execute("SELECT expires,remember FROM sessions").fetchone()
        finally:
            db.close()

    def test_remember_login_lasts_and_renews_with_use(self):
        set_password(connect, PASSWORD)
        start = time.time()
        response = self.client.post(
            "/api/login", json={"password": PASSWORD, "remember": True}, headers=HEADERS
        )
        self.assertIn(f"Max-Age={90 * 86400}", response.headers["Set-Cookie"])
        self.assertEqual(self.session_row()["remember"], 1)
        raw = self.client.get_cookie(COOKIE).value
        # 8시간이 지나도 유지됩니다. 하루가 안 지났으면 만료를 늘리지 않습니다.
        with patch("auth.time.time", return_value=start + 9 * 3600):
            response = self.client.get("/api/data")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("Set-Cookie", response.headers)
        # 60일 뒤에 쓰면 만료가 다시 90일로 늘고 같은 토큰으로 쿠키를 새로 줍니다.
        later = start + 60 * 86400
        with patch("auth.time.time", return_value=later):
            response = self.client.get("/api/data")
            self.assertEqual(response.status_code, 200)
            self.assertIn(f"{COOKIE}={raw}", response.headers["Set-Cookie"])
        self.assertGreaterEqual(self.session_row()["expires"], int(later) + 90 * 86400)
        with patch("auth.time.time", return_value=later + 89 * 86400):
            self.assertEqual(self.client.get("/api/data").status_code, 200)
        # 오래 안 쓰면 끝나고, 로그아웃하면 유지 세션도 바로 끝납니다.
        with patch("auth.time.time", return_value=later + 89 * 86400 + 91 * 86400):
            self.assertEqual(self.client.get("/api/data").status_code, 401)
        self.client.post(
            "/api/login", json={"password": PASSWORD, "remember": True}, headers=HEADERS
        )
        raw = self.client.get_cookie(COOKIE).value
        with patch("auth.time.time", return_value=time.time() + 30 * 86400):
            response = self.client.post("/api/logout", headers=HEADERS)
        self.assertIn(f"{COOKIE}=;", response.headers["Set-Cookie"])
        self.client.set_cookie(COOKIE, raw)
        self.assertEqual(self.client.get("/api/data").status_code, 401)

    def test_without_remember_stays_eight_hours_and_password_change_ends_remembered(self):
        set_password(connect, PASSWORD)
        response = self.client.post(
            "/api/login", json={"password": PASSWORD, "remember": "yes"}, headers=HEADERS
        )
        self.assertIn(f"Max-Age={8 * 3600}", response.headers["Set-Cookie"])
        self.assertEqual(self.session_row()["remember"], 0)
        self.client.post(
            "/api/login", json={"password": PASSWORD, "remember": True}, headers=HEADERS
        )
        set_password(connect, "new-fixture-password")
        self.assertEqual(self.client.get("/api/data").status_code, 401)

    def test_csrf_and_security_headers(self):
        set_password(connect, PASSWORD)
        self.assertEqual(
            self.client.post("/api/login", json={"password": PASSWORD}).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                "/api/login",
                json={"password": PASSWORD},
                headers={**HEADERS, "Origin": "https://evil.example"},
            ).status_code,
            403,
        )
        response = self.client.get("/login")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 400)

    def test_https_canonical_origin_and_secure_cookie(self):
        set_password(connect, PASSWORD)
        app.config["PUBLIC_ORIGIN"] = "https://cards.example.test"
        self.assertEqual(
            self.client.get("/login", base_url="http://cards.example.test").status_code, 400
        )
        self.assertEqual(
            self.client.get("/login", base_url="https://evil.example").status_code, 400
        )
        response = self.login(base_url="https://cards.example.test")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Secure;", response.headers["Set-Cookie"])
        self.assertIn("Strict-Transport-Security", response.headers)

    def test_password_policy(self):
        with self.assertRaises(ValueError):
            set_password(connect, "short")
