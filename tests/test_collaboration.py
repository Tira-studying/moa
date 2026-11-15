"""검증 범위: 계정 권한·동시 수정 충돌·오류 시 전체 취소.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import hashlib
import io
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from werkzeug.security import generate_password_hash
from app import app, connect, previews, secure_tables
from auth import COOKIE, set_password

HEADERS = {"X-Requested-With": "CardDashboard"}
OWNER_PASSWORD = "fixture-owner-password"
MEMBER_PASSWORD = "fixture-manager-password"


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.tmp.name) / "db.sqlite3")
        )
        set_password(connect, OWNER_PASSWORD)
        self.owner = app.test_client()
        self.login(self.owner, "owner", OWNER_PASSWORD)
        self.manager = app.test_client()

    def tearDown(self):
        previews.clear()
        secure_tables.clear()
        self.tmp.cleanup()

    def login(self, client, name, password):
        return client.post(
            "/api/login", json=dict(username=name, password=password), headers=HEADERS
        )

    def write(self, client, path, payload, method="PUT", revision=None):
        if revision is None:
            revision = client.get(
                "/api/members" if path.startswith(("/api/members", "/api/account")) else "/api/data"
            ).headers["X-Moa-Revision"]
        return client.open(
            path, method=method, json=payload, headers={**HEADERS, "If-Match": str(revision)}
        )

    def add_member(self):
        response = self.write(
            self.owner, "/api/members", dict(username="partner", password=MEMBER_PASSWORD), "POST"
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.member_id = response.json["id"]
        self.assertEqual(self.login(self.manager, "partner", MEMBER_PASSWORD).status_code, 200)

    def bill(self, amount=100):
        return dict(statement_status="received", amount=amount, due_on=None, paid=False)

    def test_legacy_owner_and_sessions_migrate_without_losing_ledger(self):
        legacy = str(Path(self.tmp.name) / "legacy.sqlite3")
        db = sqlite3.connect(legacy)
        db.executescript(
            "CREATE TABLE owner(id INTEGER PRIMARY KEY,password_hash TEXT NOT NULL);CREATE TABLE sessions(digest TEXT PRIMARY KEY,expires INTEGER NOT NULL);"
        )
        raw = "legacy-fixture-session"
        db.execute("INSERT INTO owner VALUES(1,?)", (generate_password_hash(OWNER_PASSWORD),))
        db.execute(
            "INSERT INTO sessions VALUES(?,?)",
            (hashlib.sha256(raw.encode()).hexdigest(), int(time.time()) + 3600),
        )
        db.commit()
        db.close()
        app.config["DATABASE"] = legacy
        client = app.test_client()
        client.set_cookie(COOKIE, raw)
        self.assertFalse(client.get("/api/session").json["authenticated"])
        self.assertEqual(client.get("/api/data").status_code, 401)
        self.assertEqual(self.login(client, "owner", OWNER_PASSWORD).status_code, 200)

    def test_independent_accounts_roles_revoke_and_no_secret_leak(self):
        self.add_member()
        status = self.manager.get("/api/session").json
        self.assertEqual(status["user"]["role"], "manager")
        self.assertEqual(len(self.manager.get("/api/members").json["members"]), 1)
        forbidden = self.write(
            self.manager, "/api/members", dict(username="third", password=MEMBER_PASSWORD), "POST"
        )
        self.assertEqual(forbidden.status_code, 403)
        forbidden = self.write(self.manager, "/api/members/1", dict(active=False))
        self.assertEqual(forbidden.status_code, 403)
        response = self.write(self.manager, "/api/billing/2026-10/삼성", self.bill())
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.owner.get("/api/billing?month=2026-10").json["total"], 0)
        self.assertEqual(self.manager.get("/api/billing?month=2026-10").json["total"], 100)
        audit = self.owner.get("/api/audit").json["items"]
        self.assertFalse(any(row["actor_name"] == "partner" for row in audit))
        self.assertEqual(self.manager.get("/api/audit").json["items"][0]["actor_name"], "partner")
        for response in [
            self.owner.get("/api/members"),
            self.owner.get("/api/audit"),
            self.owner.get("/api/session"),
        ]:
            self.assertNotIn("password_hash", response.text)
            self.assertNotIn(MEMBER_PASSWORD, response.text)
        raw = self.manager.get_cookie(COOKIE).value
        self.assertEqual(
            self.write(
                self.owner, f"/api/members/{self.member_id}", dict(active=False)
            ).status_code,
            200,
        )
        self.manager.set_cookie(COOKIE, raw)
        self.assertEqual(self.manager.get("/api/data").status_code, 401)
        self.assertEqual(self.login(self.manager, "partner", MEMBER_PASSWORD).status_code, 401)
        self.assertEqual(self.owner.get("/api/data").status_code, 200)

    def test_revision_conflict_missing_version_and_atomic_rollback(self):
        self.add_member()
        revision = self.owner.get("/api/data").headers["X-Moa-Revision"]
        response = self.owner.put("/api/billing/2026-10/삼성", json=self.bill(), headers=HEADERS)
        self.assertEqual(response.status_code, 428)
        self.assertEqual(
            self.write(
                self.owner, "/api/billing/2026-10/삼성", self.bill(200), revision=revision
            ).status_code,
            200,
        )
        conflict = self.write(
            self.owner, "/api/billing/2026-10/삼성", self.bill(300), revision=revision
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["code"], "revision_conflict")
        self.assertEqual(self.owner.get("/api/billing?month=2026-10").json["total"], 200)
        audit = self.owner.get("/api/audit").json["items"]
        self.assertEqual(len(audit), 2)
        current = self.owner.get("/api/data").headers["X-Moa-Revision"]
        self.assertEqual(
            self.write(
                self.owner, "/api/billing/2026-10/삼성", self.bill(-1), revision=current
            ).status_code,
            400,
        )
        self.assertEqual(self.owner.get("/api/data").headers["X-Moa-Revision"], current)

    def test_concurrent_writes_only_one_wins(self):
        self.add_member()
        revision = self.owner.get("/api/data").headers["X-Moa-Revision"]
        cookies = [self.owner.get_cookie(COOKIE).value, self.owner.get_cookie(COOKIE).value]

        def save(args):
            index, raw = args
            client = app.test_client()
            client.set_cookie(COOKIE, raw)
            return self.write(
                client, "/api/billing/2026-10/삼성", self.bill(100 + index), revision=revision
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(save, enumerate(cookies)))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(len(self.owner.get("/api/audit").json["items"]), 2)

    def test_password_change_and_owner_reset_revoke_only_target_account(self):
        self.add_member()
        old = self.manager.get_cookie(COOKIE).value
        self.assertEqual(
            self.write(
                self.manager,
                "/api/account/password",
                dict(current_password="wrong", password="new-fixture-password"),
            ).status_code,
            400,
        )
        self.assertEqual(
            self.write(
                self.manager,
                "/api/account/password",
                dict(current_password=MEMBER_PASSWORD, password="new-fixture-password"),
            ).status_code,
            200,
        )
        self.manager.set_cookie(COOKIE, old)
        self.assertEqual(self.manager.get("/api/data").status_code, 401)
        self.assertEqual(
            self.login(self.manager, "partner", "new-fixture-password").status_code, 200
        )
        self.assertEqual(self.owner.get("/api/data").status_code, 200)
        self.assertEqual(
            self.write(
                self.owner,
                f"/api/members/{self.member_id}",
                dict(password="reset-fixture-password"),
            ).status_code,
            200,
        )
        self.assertEqual(self.manager.get("/api/data").status_code, 401)
        self.assertEqual(
            self.login(self.manager, "partner", "reset-fixture-password").status_code, 200
        )
        self.assertEqual(
            self.write(
                self.owner,
                "/api/account/password",
                dict(current_password=OWNER_PASSWORD, password="new-owner-fixture"),
            ).status_code,
            200,
        )
        self.assertEqual(self.owner.get("/api/data").status_code, 401)
        self.assertEqual(self.manager.get("/api/data").status_code, 200)
        self.assertEqual(self.login(self.owner, "owner", "new-owner-fixture").status_code, 200)

    def test_preview_tokens_are_private_to_creating_user(self):
        self.add_member()
        response = self.owner.post(
            "/api/preview",
            data={
                "file": (
                    io.BytesIO("날짜,가맹점,금액\n2026-10-01,가상점,100".encode()),
                    "fixture.csv",
                ),
                "header": "1",
            },
            headers=HEADERS,
        )
        token = response.json["token"]
        payload = dict(token=token, card="삼성", date=0, merchant=1, amount=2)
        self.assertEqual(self.write(self.manager, "/api/import", payload, "POST").status_code, 400)
        self.assertEqual(self.write(self.owner, "/api/import", payload, "POST").status_code, 200)
        self.assertEqual(self.owner.get("/api/audit").json["items"][0]["target"], "명세서 #1")
        secure_tables["owner-token"] = dict(owner_id=1, tables=[["fixture"]], digest="fixture")
        self.assertEqual(
            self.manager.post(
                "/api/secure-preview", json={"token": "owner-token"}, headers=HEADERS
            ).status_code,
            400,
        )

    def test_member_limit_validation_and_reactivation(self):
        self.add_member()
        self.assertEqual(
            self.write(
                self.owner, "/api/members", dict(username="third", password=MEMBER_PASSWORD), "POST"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.write(self.owner, "/api/members/1", dict(active=False)).status_code, 404
        )
        self.assertEqual(
            self.write(
                self.owner, f"/api/members/{self.member_id}", dict(active=False)
            ).status_code,
            200,
        )
        self.assertEqual(
            self.write(
                self.owner, "/api/members", dict(username="other", password=MEMBER_PASSWORD), "POST"
            ).status_code,
            200,
        )
        self.assertEqual(
            self.write(self.owner, f"/api/members/{self.member_id}", dict(active=True)).status_code,
            400,
        )
        self.assertEqual(
            self.write(
                self.owner,
                "/api/members",
                dict(username="<script>", password=MEMBER_PASSWORD),
                "POST",
            ).status_code,
            400,
        )
        self.assertEqual(
            self.write(
                self.owner, "/api/members", dict(username="safe", password="short"), "POST"
            ).status_code,
            400,
        )

    def test_error_after_ledger_insert_rolls_back_data_revision_and_audit(self):
        self.add_member()
        response = self.owner.post(
            "/api/preview",
            data={
                "file": (
                    io.BytesIO("날짜,가맹점,금액\n2026-10-01,가상점,100".encode()),
                    "fixture.csv",
                ),
                "header": "1",
            },
            headers=HEADERS,
        )
        payload = dict(token=response.json["token"], card="삼성", date=0, merchant=1, amount=2)
        revision = self.owner.get("/api/data").headers["X-Moa-Revision"]
        with patch(
            "app.finance.new_duplicate_count",
            side_effect=RuntimeError("fixture failure after insertion"),
        ):
            with self.assertRaises(RuntimeError):
                self.write(self.owner, "/api/import", payload, "POST", revision=revision)
        response = self.owner.get("/api/data")
        self.assertEqual(response.json["transactions"], [])
        self.assertEqual(response.json["imports"], [])
        self.assertEqual(response.headers["X-Moa-Revision"], revision)
        self.assertEqual(len(self.owner.get("/api/audit").json["items"]), 1)
