"""개인 원장 권한, 합계 전용 응답, 이전 원자성, 계정별 메일을 가상 자료로 검증합니다."""

import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import app, connect
from auth import set_password, auth_schema
from categories import schema as category_schema
import finance
import gmail_inbox as mail
import personal_data
from personal_data import personal_scope, current_user_id
from mail_worker import tick_users

HEADERS = {"X-Requested-With": "CardDashboard"}


class PersonalDataTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True,
            PUBLIC_ORIGIN="",
            DATABASE=str(Path(self.directory.name) / "personal.sqlite3"),
        )
        set_password(connect, "fixture-owner-password")
        self.owner = app.test_client()
        self.owner.post("/api/login", json={"password": "fixture-owner-password"}, headers=HEADERS)
        self.write(
            self.owner,
            "/api/members",
            {"username": "partner", "password": "fixture-partner-password"},
            "POST",
        )
        self.partner = app.test_client()
        self.partner.post(
            "/api/login",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers=HEADERS,
        )

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def write(self, client, path, payload=None, method="PUT", revision=None):
        if revision is None:
            endpoint = (
                "/api/members" if path.startswith(("/api/members", "/api/account")) else "/api/data"
            )
            revision = client.get(endpoint).headers["X-Moa-Revision"]
        return client.open(
            path, method=method, json=payload, headers={**HEADERS, "If-Match": revision}
        )

    def upload(self, client, filename, rows, card="삼성"):
        contents = "이용일,가맹점,금액\n" + "\n".join(",".join(map(str, row)) for row in rows)
        preview = client.post(
            "/api/preview",
            data={"file": (io.BytesIO(contents.encode()), filename), "header": "1"},
            headers=HEADERS,
        )
        self.assertEqual(preview.status_code, 200, preview.json)
        result = self.write(
            client,
            "/api/import",
            dict(token=preview.json["token"], card=card, date=0, merchant=1, amount=2),
            "POST",
        )
        self.assertEqual(result.status_code, 200, result.json)
        return client.get("/api/data").json

    def test_personal_lists_private_ids_and_aggregate_contains_only_sums(self):
        own = self.upload(
            self.owner,
            "owner-private.csv",
            [
                ("2026-11-01", "스타벅스_가상비공개A", 1000),
                ("2026-11-02", "이마트_가상비공개A", 2000),
                ("2026-11-02", "이마트_가상비공개A", -200),
            ],
        )
        other = self.upload(
            self.partner,
            "partner-private.csv",
            [("2026-11-03", "가상의원_비공개B", 5000), ("2026-11-04", "네이버페이", 600)],
        )
        target = next(row["id"] for row in other["transactions"] if row["merchant"] == "네이버페이")
        self.assertEqual(
            self.write(
                self.partner,
                f"/api/transactions/{target}/category",
                dict(
                    category="식비",
                    scope="transaction",
                    actual_merchant="가상개인식당B",
                    memo="비공개메모B",
                ),
            ).status_code,
            200,
        )
        for client, marker in [(self.owner, "비공개B"), (self.partner, "비공개A")]:
            for path in ["/api/data", "/api/finance?month=2026-11", "/api/audit"]:
                response = client.get(path)
                self.assertNotIn(marker, response.text)
                self.assertNotIn(
                    "비공개메모B" if client is self.owner else "owner-private.csv", response.text
                )
        for client in (self.owner, self.partner):
            response = client.get("/api/household?month=2026-11")
            self.assertEqual(response.status_code, 200)
            data = response.json
            self.assertEqual(
                (data["net"], data["gross"], data["refunds"], data["count"]), (8400, 8600, 200, 5)
            )
            self.assertEqual(
                {row["name"]: row["amount"] for row in data["categories"]},
                {"의료": 5000, "마트·식료품": 2000, "커피": 1000, "식비": 600},
            )
            self.assertEqual(data["cards"], [{"name": "삼성", "amount": 8600}])
            self.assertEqual(
                set(data),
                {"month", "net", "gross", "refunds", "count", "categories", "cards", "trend"},
            )
            for forbidden in [
                "비공개",
                "메모",
                ".csv",
                "merchant",
                "owner_id",
                "transaction_id",
                "partner",
                "owner",
            ]:
                self.assertNotIn(forbidden, response.text)
        self.assertEqual(self.owner.get("/api/household?month=2026-13").status_code, 400)
        self.assertEqual(app.test_client().get("/api/household?month=2026-11").status_code, 401)
        # 소유자도 상대방의 개인 행을 직접 수정·삭제할 수 없습니다.
        for client, data in [(self.owner, other), (self.partner, own)]:
            transaction_id = data["transactions"][0]["id"]
            for path, payload, method in [
                (f"/api/transactions/{transaction_id}/category", {"category": "식비"}, "PUT"),
                (f"/api/transactions/{transaction_id}/exclusion", {"excluded": False}, "PUT"),
                (f"/api/transactions/{transaction_id}/recurring", {"status": "confirmed"}, "PUT"),
                (f'/api/imports/{data["imports"][0]["id"]}', None, "DELETE"),
            ]:
                self.assertEqual(self.write(client, path, payload, method).status_code, 404, path)

    def test_same_file_and_merchant_preferences_are_independent(self):
        rows = [("2026-11-01", "동일가상가맹점", 300)]
        owner = self.upload(self.owner, "same.csv", rows)
        partner = self.upload(self.partner, "same.csv", rows)
        for client, data, category in [
            (self.owner, owner, "커피"),
            (self.partner, partner, "의료"),
        ]:
            response = self.write(
                client,
                f'/api/transactions/{data["transactions"][0]["id"]}/category',
                {"category": category},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/api/data").json["transactions"][0]["category"], category)
            self.assertEqual(client.get("/api/finance?month=2026-11").json["duplicates"], [])
        self.assertEqual(self.owner.get("/api/data").json["transactions"][0]["category"], "커피")
        aggregate = self.owner.get("/api/household?month=2026-11").json
        self.assertEqual(aggregate["net"], 600)
        self.assertEqual({row["name"] for row in aggregate["categories"]}, {"커피", "의료"})
        own_id = owner["transactions"][0]["id"]
        self.assertEqual(
            self.write(
                self.owner, f"/api/transactions/{own_id}/exclusion", {"excluded": True}
            ).status_code,
            409,
        )

    def test_previous_accounts_page_cannot_write_under_a_new_session(self):
        revision = self.partner.get("/api/data").headers["X-Moa-Revision"]
        response = self.partner.put(
            "/api/billing/2026-11/삼성",
            json=dict(statement_status="received", amount=999, due_on=None, paid=False),
            headers={**HEADERS, "If-Match": revision, "X-Moa-User": "1"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "account_changed")
        self.assertEqual(self.partner.get("/api/billing?month=2026-11").json["total"], 0)
        self.assertEqual(
            self.partner.get("/api/data", headers={"X-Moa-User": "1"}).status_code, 409
        )
        self.assertEqual(
            self.partner.get("/api/data", headers={"X-Moa-User": "2"}).status_code, 200
        )

    def test_personal_bills_plans_and_revisions_do_not_cross_users(self):
        owner_revision = self.owner.get("/api/data").headers["X-Moa-Revision"]
        partner_revision = self.partner.get("/api/data").headers["X-Moa-Revision"]
        plan = dict(
            name="가상개인할부B",
            card="우리",
            principal=900,
            months=3,
            first_month="2026-11",
            paid_count=0,
        )
        result = self.write(self.partner, "/api/installments", plan, "POST", partner_revision)
        self.assertEqual(result.status_code, 200)
        plan_id = result.json["id"]
        self.assertEqual(
            self.owner.get("/api/finance?month=2026-11").json["installments"]["plans"], []
        )
        self.assertEqual(
            self.write(self.owner, f"/api/installments/{plan_id}", plan).status_code, 404
        )
        self.assertEqual(
            self.write(self.owner, f"/api/installments/{plan_id}", method="DELETE").status_code, 404
        )
        self.assertEqual(
            self.write(
                self.partner,
                "/api/billing/2026-11/삼성",
                dict(statement_status="received", amount=700, due_on=None, paid=False),
            ).status_code,
            200,
        )
        self.assertEqual(self.owner.get("/api/billing?month=2026-11").json["total"], 0)
        self.assertEqual(self.partner.get("/api/billing?month=2026-11").json["total"], 700)
        self.assertEqual(
            self.write(
                self.partner, "/api/billing/settings/삼성", {"enabled": False, "due_day": 15}
            ).status_code,
            200,
        )
        self.assertTrue(
            next(
                row
                for row in self.owner.get("/api/billing?month=2026-11").json["cards"]
                if row["card"] == "삼성"
            )["enabled"]
        )
        self.assertEqual(self.owner.get("/api/data").headers["X-Moa-Revision"], owner_revision)
        self.assertNotEqual(
            self.partner.get("/api/data").headers["X-Moa-Revision"], partner_revision
        )
        self.assertNotIn("개인할부B", self.owner.get("/api/audit").text)

    def test_personal_mail_never_falls_back_to_owners_mail_or_password(self):
        root = Path(self.directory.name) / "secrets"
        root.mkdir()
        app_password = root / "owner-app"
        app_password.write_text("abcdefghijklmnop")
        common = root / "owner-statement"
        common.write_text("fixture-owner-statement")
        with patch.dict(
            os.environ,
            {
                "MOA_GMAIL_USER": "owner@example.test",
                "MOA_GMAIL_PASSWORD_FILE": str(app_password),
                "MOA_STATEMENT_PASSWORD_FILE": str(common),
                "MOA_PERSONAL_SECRETS_DIR": str(root / "users"),
            },
        ):
            with personal_scope(1):
                owner_key = mail.config()[3]
            state = self.partner.get("/api/mail").json
            self.assertFalse(state["configured"])
            self.assertEqual(state["items"], [])
            with personal_scope(2):
                self.assertIsNone(mail.common_password())
            folder = root / "users" / "2"
            folder.mkdir(parents=True)
            (folder / "gmail.json").write_text(
                json.dumps({"user": "partner@example.test", "folder": "MOA"})
            )
            (folder / "gmail-app-password").write_text("ponmlkjihgfedcba")
            (folder / "statement-password").write_text("fixture-partner-statement")
            with personal_scope(2):
                partner_key = mail.config()[3]
                self.assertEqual(mail.common_password(), "fixture-partner-statement")
            self.assertNotEqual(owner_key, partner_key)
            db = connect()
            mail.schema(db)
            with db:
                for identity, key, owner_id in [
                    ("owner-mail", owner_key, 1),
                    ("partner-mail", partner_key, 2),
                ]:
                    db.execute(
                        "INSERT INTO mail_candidates(id,mailbox,validity,uid,part,filename,card,size,owner_id,digest) VALUES(?,?,'1',1,1,?,'삼성',10,?,'same-digest')",
                        (identity, key, identity + ".html", owner_id),
                    )
            db.close()
            self.assertEqual(self.partner.get("/api/mail").json["items"][0]["id"], "partner-mail")
            self.assertEqual(self.owner.get("/api/mail").json["items"][0]["id"], "owner-mail")
            # 실제 Gmail에 접속하기 전에 후보 소유권에서 거절해야 합니다.
            with patch("gmail_inbox.session") as network:
                self.assertEqual(
                    self.owner.post(
                        "/api/mail/partner-mail/attachment", headers=HEADERS
                    ).status_code,
                    400,
                )
                self.assertEqual(
                    self.partner.post(
                        "/api/mail/owner-mail/attachment", headers=HEADERS
                    ).status_code,
                    400,
                )
                network.assert_not_called()
            with personal_scope(2):
                mail.mark_failed(connect, "same-digest")
            db = connect()
            self.assertIsNone(
                db.execute(
                    "SELECT auto_error FROM mail_candidates WHERE id='owner-mail'"
                ).fetchone()[0]
            )
            self.assertIsNotNone(
                db.execute(
                    "SELECT auto_error FROM mail_candidates WHERE id='partner-mail'"
                ).fetchone()[0]
            )
            db.close()

    def test_gmail_sync_assigns_candidates_to_processing_user(self):
        from test_gmail_inbox import FakeImap

        root = Path(self.directory.name) / "users"
        folder = root / "2"
        folder.mkdir(parents=True)
        (folder / "gmail.json").write_text(json.dumps({"user": "partner@example.test"}))
        (folder / "gmail-app-password").write_text("abcdefghijklmnop")
        with (
            patch.dict(os.environ, {"MOA_PERSONAL_SECRETS_DIR": str(root)}),
            patch("gmail_inbox.imaplib.IMAP4_SSL", return_value=FakeImap()),
        ):
            with personal_scope(2):
                self.assertEqual(mail.sync(connect), 2)
                self.assertEqual(len(mail.inbox(connect)["items"]), 2)
        db = connect()
        self.assertEqual(
            [row[0] for row in db.execute("SELECT owner_id FROM mail_candidates")], [2, 2]
        )
        db.close()

    def test_worker_visits_active_users_with_separate_contexts(self):
        visited = []
        with patch(
            "mail_worker.tick",
            side_effect=lambda connector, when: visited.append(current_user_id()) or 123,
        ):
            result = tick_users(connect, {})
        self.assertEqual(visited, [1, 2])
        self.assertEqual(result, {1: 123, 2: 123})
        self.assertEqual(current_user_id(), 1)
        self.assertEqual(
            self.write(self.owner, "/api/members/2", {"active": False}).status_code, 200
        )
        visited = []
        with patch(
            "mail_worker.tick",
            side_effect=lambda connector, when: visited.append(current_user_id()) or 456,
        ):
            result = tick_users(connect, result)
        self.assertEqual(visited, [1])
        self.assertEqual(result, {1: 456})

    def test_excluded_transactions_and_disabled_accounts_are_not_aggregated(self):
        first = self.upload(self.owner, "first.csv", [("2026-11-01", "가상카페A", 100)])
        second = self.upload(
            self.owner,
            "second.csv",
            [("2026-11-01", "가상카페A", 100), ("2026-11-02", "가상추가A", 1)],
        )
        self.upload(self.partner, "partner.csv", [("2026-11-01", "가상병원B", 500)], card="신한")
        self.assertEqual(
            self.write(
                self.owner,
                f'/api/transactions/{first["transactions"][0]["id"]}/exclusion',
                {"excluded": True},
            ).status_code,
            200,
        )
        self.assertEqual(self.owner.get("/api/household?month=2026-11").json["net"], 601)
        self.write(self.owner, "/api/members/2", {"active": False})
        self.assertEqual(self.owner.get("/api/household?month=2026-11").json["net"], 101)
        self.write(self.owner, "/api/members/2", {"active": True})
        self.assertEqual(self.owner.get("/api/household?month=2026-11").json["net"], 601)


class PersonalMailSetupTests(unittest.TestCase):
    def setUp(self):
        import runpy

        self.save = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts" / "configure-personal-mail.py")
        )["save_configuration"]

    def test_writes_only_personal_directory_with_private_modes_and_preserves_blank_password(self):
        with tempfile.TemporaryDirectory() as directory, patch("os.chown"):
            root = Path(directory)
            self.save(
                root,
                2,
                "partner@example.test",
                "MOA",
                "abcd efgh ijkl mnop",
                "fixture-personal-password",
            )
            folder = root / "users" / "2"
            self.assertEqual((folder / "gmail-app-password").read_text(), "abcdefghijklmnop")
            self.assertEqual(
                (folder / "statement-password").read_text(), "fixture-personal-password"
            )
            self.assertEqual((folder / "gmail.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            self.save(root, 2, "partner@example.test", "MOA", "abcdefghijklmnop")
            self.assertEqual(
                (folder / "statement-password").read_text(), "fixture-personal-password"
            )
            self.assertFalse((root / "gmail-app-password").exists())

    def test_invalid_inputs_do_not_create_secret_files(self):
        with tempfile.TemporaryDirectory() as directory, patch("os.chown"):
            root = Path(directory)
            for number, user, password in [
                (1, "partner@example.test", "abcdefghijklmnop"),
                (2, "bad address", "abcdefghijklmnop"),
                (2, "partner@example.test", "short"),
            ]:
                with self.assertRaises(ValueError):
                    self.save(root, number, user, "MOA", password)
            self.assertEqual(list(root.iterdir()), [])


class PersonalMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(str(Path(self.directory.name) / "legacy.sqlite3"))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """CREATE TABLE imports(id INTEGER PRIMARY KEY,card TEXT NOT NULL,filename TEXT NOT NULL,digest TEXT NOT NULL,created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(card,digest));
        CREATE TABLE transactions(id INTEGER PRIMARY KEY,import_id INTEGER REFERENCES imports(id) ON DELETE CASCADE,used_on TEXT,merchant TEXT,amount INTEGER);
        CREATE TABLE merchant_categories(merchant_key TEXT PRIMARY KEY,category TEXT NOT NULL);"""
        )
        finance.schema(self.db)
        category_schema(self.db)
        auth_schema(self.db)
        with self.db:
            self.db.execute(
                "INSERT INTO imports(id,card,filename,digest) VALUES(7,'삼성','legacy.csv','legacy')"
            )
            self.db.execute(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(101,7,'2026-11-01','가상기존상점',100)"
            )
            self.db.execute(
                "INSERT INTO transaction_annotations VALUES(101,'식비','가상상점','가상메모')"
            )
            self.db.execute("INSERT INTO merchant_categories VALUES('가상상점','식비')")
            self.db.execute(
                "INSERT INTO monthly_bills VALUES('2026-11','삼성','received',100,NULL,0)"
            )
            self.db.execute(
                "INSERT INTO sessions(digest,expires) VALUES('legacy-session',9999999999)"
            )
        self.db.execute("PRAGMA foreign_keys=ON")

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_legacy_ownership_and_links_preserved_and_migration_is_idempotent(self):
        personal_data.schema(self.db)
        personal_data.schema(self.db)
        self.assertEqual(
            self.db.execute("SELECT owner_id FROM imports WHERE id=7").fetchone()[0], 1
        )
        self.assertEqual(
            tuple(
                self.db.execute("SELECT import_id,amount FROM transactions WHERE id=101").fetchone()
            ),
            (7, 100),
        )
        self.assertEqual(
            self.db.execute(
                "SELECT memo FROM transaction_annotations WHERE transaction_id=101"
            ).fetchone()[0],
            "가상메모",
        )
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT owner_id FROM monthly_bills").fetchone()[0], 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        with self.db:
            self.db.execute(
                "INSERT INTO imports(card,filename,digest,owner_id) VALUES('삼성','same.csv','legacy',2)"
            )
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 2)

    def test_failure_rolls_back_entire_migration_and_restores_foreign_key_checks(self):
        class FailingConnection:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, *args):
                if sql == "DROP TABLE monthly_bills":
                    raise sqlite3.OperationalError("fixture migration failure")
                return self.db.execute(sql, *args)

            def __getattr__(self, key):
                return getattr(self.db, key)

        with self.assertRaises(sqlite3.OperationalError):
            personal_data.schema(FailingConnection(self.db))
        self.assertNotIn(
            "owner_id", {row[1] for row in self.db.execute("PRAGMA table_info(imports)")}
        )
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        personal_data.schema(self.db)
