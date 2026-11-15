"""가상 두 계정으로 개인 분야 설정·분류 연속성·공유 합계·동시 수정과 기존 DB 보존을 검증합니다."""

import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import quote

from app import app, connect
from auth import set_password
from categories import classify

HEADERS = {"X-Requested-With": "CardDashboard"}


class CategorySettingsTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.directory.name) / "db.sqlite3")
        )
        set_password(connect, "fixture-owner-password")
        self.owner = app.test_client()
        self.owner.post("/api/login", json={"password": "fixture-owner-password"}, headers=HEADERS)
        revision = self.owner.get("/api/members").headers["X-Moa-Revision"]
        self.owner.post(
            "/api/members",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers={**HEADERS, "If-Match": revision},
        )
        self.partner = app.test_client()
        self.partner.post(
            "/api/login",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers=HEADERS,
        )
        db = connect()
        with db:
            for owner in (1, 2):
                db.execute(
                    "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(?,'삼성','fixture',?,?)",
                    (owner, f"fixture-{owner}", owner),
                )
            db.executemany(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(?,?,?,?,?)",
                [
                    (1, 1, "2026-11-01", "가상가족상점", 1000),
                    (2, 1, "2026-11-02", "가상어린이집", 2000),
                    (3, 1, "2026-11-03", "가상구독상점", -100),
                    (4, 2, "2026-11-02", "가상어린이집", 3000),
                ],
            )
        db.close()

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def write(self, client, path, body=None, method="POST", revision=None, extra=None):
        """편집 시작 시점의 개인 버전을 보내며 누락·충돌 검사는 별도로 수행합니다."""
        if revision is None:
            revision = client.get("/api/categories").headers["X-Moa-Revision"]
        return client.open(
            path,
            method=method,
            json=body,
            headers={**HEADERS, "If-Match": revision, **(extra or {})},
        )

    def create(self, name="반려동물", client=None):
        response = self.write(client or self.owner, "/api/categories", {"name": name})
        self.assertEqual(response.status_code, 200, response.json)
        return next(row for row in response.json["items"] if row["name"] == name)

    def path(self, key):
        return "/api/categories/" + quote(key, safe="")

    def test_new_defaults_and_specific_rules_keep_manual_priority(self):
        categories = self.owner.get("/api/data").json["categories"]
        self.assertIn("육아", categories)
        self.assertIn("구독료", categories)
        for merchant, expected in [
            ("가상키즈카페", "육아"),
            ("가상어린이집", "육아"),
            ("NETFLIX", "구독료"),
            ("가상유튜브프리미엄", "구독료"),
            ("가상 카페", "커피"),
            ("가상상점", "미분류"),
        ]:
            self.assertEqual(classify(merchant, {})[0], expected)
        self.assertEqual(classify("넷플릭스", {"넷플릭스": "문화·여가"}), ("문화·여가", "manual"))

    def test_personal_add_assign_rename_and_aggregate_preserve_keys_amounts_and_notes(self):
        row = self.create()
        response = self.write(
            self.owner,
            "/api/transactions/1/category",
            {"category": row["name"], "scope": "transaction", "memo": "가상 메모"},
            "PUT",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.write(self.owner, self.path(row["key"]), {"name": "동물 돌봄"}, "PUT").status_code,
            200,
        )
        rows = {item["id"]: item for item in self.owner.get("/api/data").json["transactions"]}
        self.assertEqual(
            (rows[1]["category"], rows[1]["memo"], rows[1]["amount"]),
            ("동물 돌봄", "가상 메모", 1000),
        )
        db = connect()
        self.assertEqual(
            db.execute(
                "SELECT category FROM transaction_annotations WHERE transaction_id=1"
            ).fetchone()[0],
            row["key"],
        )
        db.close()
        aggregate = self.partner.get("/api/household?month=2026-11").json
        self.assertEqual(aggregate["net"], 5900)
        self.assertEqual(
            {item["name"]: item["amount"] for item in aggregate["categories"]},
            {"육아": 5000, "동물 돌봄": 1000},
        )
        self.assertNotIn(row["key"], str(aggregate))
        self.assertNotIn("가상 메모", str(aggregate))

    def test_default_rename_changes_own_auto_manual_shared_labels_and_future_transactions(self):
        self.write(self.owner, "/api/transactions/1/category", {"category": "육아"}, "PUT")
        self.assertEqual(
            self.write(self.owner, self.path("육아"), {"name": "아이 돌봄"}, "PUT").status_code, 200
        )
        self.assertEqual(
            {
                row["category"]
                for row in self.owner.get("/api/data").json["transactions"]
                if row["id"] in (1, 2)
            },
            {"아이 돌봄"},
        )
        self.assertEqual(self.partner.get("/api/data").json["transactions"][0]["category"], "육아")
        db = connect()
        with db:
            db.execute(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(5,1,'2026-11-04','다른어린이집',500)"
            )
        db.close()
        self.assertEqual(
            self.owner.get("/api/data").json["transactions"][0]["category"], "아이 돌봄"
        )
        shared = self.partner.get("/api/shared-management?month=2026-11").json
        owner = next(item for item in shared["members"] if item["username"] == "owner")
        self.assertEqual(owner["rules"][0]["category"], "아이 돌봄")
        aggregate = self.owner.get("/api/household?month=2026-11").json
        self.assertEqual(
            {row["name"]: row["amount"] for row in aggregate["categories"]},
            {"아이 돌봄": 3500, "육아": 3000},
        )
        # 이름을 바꾼 기본 분야의 원래 이름을 사용자 분야로 재사용해도 두 분류 키는 다릅니다.
        custom = self.create("육아")
        self.write(self.owner, "/api/transactions/1/category", {"category": "육아"}, "PUT")
        db = connect()
        self.assertEqual(
            db.execute("SELECT category FROM merchant_categories WHERE owner_id=1").fetchone()[0],
            custom["key"],
        )
        db.close()

    def test_foreign_custom_categories_and_personal_revisions_are_isolated(self):
        own = self.create("나의 분야")
        revision = self.owner.get("/api/categories").headers["X-Moa-Revision"]
        self.create("상대 분야", self.partner)
        self.assertEqual(
            self.write(
                self.owner, self.path(own["key"]), {"name": "내 분야 변경"}, "PUT", revision
            ).status_code,
            200,
        )
        self.assertNotIn("내 분야 변경", self.partner.get("/api/categories").text)
        for method in ("PUT", "DELETE"):
            self.assertEqual(
                self.write(
                    self.partner, self.path(own["key"]), {"name": "가로채기"}, method
                ).status_code,
                404,
            )
        self.assertEqual(
            self.write(
                self.partner, "/api/transactions/4/category", {"category": "내 분야 변경"}, "PUT"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.write(
                self.partner, "/api/transactions/1/category", {"category": "육아"}, "PUT"
            ).status_code,
            404,
        )

    def test_used_custom_category_and_future_merchant_rule_cannot_be_deleted(self):
        row = self.create()
        self.write(self.owner, "/api/transactions/1/category", {"category": row["name"]}, "PUT")
        self.assertEqual(
            self.write(self.owner, self.path(row["key"]), method="DELETE").status_code, 400
        )
        # 거래를 삭제했더라도 향후 거래에 적용할 가맹점 규칙은 유지되어야 합니다.
        db = connect()
        with db:
            db.execute("DELETE FROM transactions WHERE id=1")
        db.close()
        self.assertEqual(
            self.write(self.owner, self.path(row["key"]), method="DELETE").status_code, 400
        )
        db = connect()
        with db:
            db.execute("DELETE FROM merchant_categories WHERE owner_id=1")
        db.close()
        self.assertEqual(
            self.write(self.owner, self.path(row["key"]), method="DELETE").status_code, 200
        )
        self.assertNotIn(row["name"], self.owner.get("/api/data").json["categories"])
        self.assertEqual(
            self.write(self.owner, self.path("육아"), method="DELETE").status_code, 400
        )

    def test_validation_limits_and_failed_writes_do_not_advance_revision(self):
        self.create("My Pet")
        for name in (
            None,
            12,
            [],
            "",
            "  ",
            "x" * 25,
            "줄\n바꿈",
            "숨김\u200b문자",
            "<script>",
            "__auto__",
            "육아",
            "ｍｙｐｅｔ",
        ):
            before = self.owner.get("/api/categories").headers["X-Moa-Revision"]
            response = self.write(self.owner, "/api/categories", {"name": name})
            self.assertEqual(response.status_code, 400, name)
            self.assertEqual(self.owner.get("/api/categories").headers["X-Moa-Revision"], before)
        self.assertEqual(
            self.write(self.owner, self.path("미분류"), {"name": "이름 변경"}, "PUT").status_code,
            400,
        )
        self.assertEqual(self.write(self.owner, "/api/categories", []).status_code, 400)
        for index in range(29):
            self.create(f"가상 분야 {index}")
        self.assertEqual(
            self.write(self.owner, "/api/categories", {"name": "한도 초과"}).status_code, 400
        )

    def test_auth_origin_stale_page_and_audit_guards_cover_settings_writes(self):
        self.assertEqual(app.test_client().get("/api/categories").status_code, 401)
        self.assertEqual(
            app.test_client()
            .post("/api/categories", json={"name": "가상"}, headers=HEADERS)
            .status_code,
            401,
        )
        self.assertEqual(self.owner.post("/api/categories", json={"name": "가상"}).status_code, 403)
        self.assertEqual(
            self.owner.post("/api/categories", json={"name": "가상"}, headers=HEADERS).status_code,
            428,
        )
        revision = self.owner.get("/api/categories").headers["X-Moa-Revision"]
        self.create()
        self.assertEqual(
            self.write(
                self.owner, "/api/categories", {"name": "충돌"}, revision=revision
            ).status_code,
            409,
        )
        for extra, status in [
            ({"Origin": "https://other.example"}, 403),
            ({"X-Moa-User": "2"}, 409),
        ]:
            self.assertEqual(
                self.write(
                    self.owner, "/api/categories", {"name": "실패"}, extra=extra
                ).status_code,
                status,
            )
        audit = str(self.owner.get("/api/audit").json)
        self.assertIn("개인 소비 분야 추가", audit)
        self.assertNotIn("반려동물", audit)
        self.assertEqual(self.owner.get("/api/categories").headers["Cache-Control"], "no-store")

    def test_schema_upgrade_keeps_existing_annotation_rules_and_transaction_identity(self):
        self.write(
            self.owner,
            "/api/transactions/1/category",
            {"category": "식비", "scope": "transaction", "memo": "가상 메모"},
            "PUT",
        )
        self.write(self.owner, "/api/transactions/2/category", {"category": "교육"}, "PUT")
        db = connect()
        tables = ("imports", "transactions", "transaction_annotations", "merchant_categories")
        before = {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] for table in tables
        }
        with db:
            db.execute("DROP TABLE spending_categories")
        db.close()
        db = connect()
        self.assertEqual(
            before,
            {
                table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                for table in tables
            },
        )
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        db.close()

    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_mobile_category_management_and_transaction_flow(self):
        """가상 원장으로 실제 추가·이름 변경·분류·새로고침과 작은 화면 배치를 확인합니다."""
        from werkzeug.serving import make_server, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                ["node", "tests/category-settings-ui.cjs"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
                check=True,
                timeout=60,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
