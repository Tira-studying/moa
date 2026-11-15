"""신규 내역의 분야 확인 알림·확인 범위·개인 권한·기존 DB 도입 경계를 가상 거래로 검증합니다."""

from pathlib import Path
import os
import subprocess
import tempfile
import threading
import unittest

from app import app, connect
from auth import set_password

HEADERS = {"X-Requested-With": "CardDashboard"}


class CategoryReviewTests(unittest.TestCase):
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
            db.executemany(
                "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(?,'삼성','fixture',?,?)",
                [(1, "fixture-1", 1), (2, "fixture-2", 2)],
            )
            db.executemany(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(?,?,?,?,?)",
                [
                    (1, 1, "2026-11-01", "가상카페", 1000),
                    (2, 1, "2026-11-02", "가상상점", 2000),
                    (3, 2, "2026-11-01", "다른가상상점", 3000),
                ],
            )
        db.close()

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def write(self, client, path, body, revision=None):
        revision = revision or client.get("/api/data").headers["X-Moa-Revision"]
        return client.put(path, json=body, headers={**HEADERS, "If-Match": revision})

    def count(self, client=None):
        return (client or self.owner).get("/api/category-reviews").json["pending_count"]

    def test_new_automatic_categories_need_review_and_explicit_ids_leave_later_arrivals_pending(
        self,
    ):
        self.assertEqual(self.count(), 2)
        rows = {row["id"]: row for row in self.owner.get("/api/data").json["transactions"]}
        self.assertEqual(rows[1]["category"], "커피")
        self.assertFalse(rows[1]["category_reviewed"])
        db = connect()
        with db:
            db.execute(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(4,1,'2026-11-03','새가상상점',500)"
            )
        db.close()
        self.assertEqual(
            self.write(self.owner, "/api/category-reviews", {"ids": [1, 2]}).status_code, 200
        )
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.count(self.partner), 1)
        rows = {row["id"]: row for row in self.owner.get("/api/data").json["transactions"]}
        self.assertTrue(rows[1]["category_reviewed"])
        self.assertFalse(rows[4]["category_reviewed"])
        self.assertEqual(sum(row["amount"] for row in rows.values()), 3500)

    def test_category_edit_confirms_only_target_and_memo_edit_does_not_confirm(self):
        self.write(
            self.owner,
            "/api/transactions/1/category",
            {"category": None, "memo": "가상메모", "update_category": False},
        )
        self.assertEqual(self.count(), 2)
        self.write(self.owner, "/api/transactions/1/category", {"category": "구독료"})
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.owner.get("/api/data").json["transactions"][-1]["memo"], "가상메모")
        db = connect()
        with db:
            db.execute("INSERT INTO transaction_exclusions(transaction_id) VALUES(2)")
        db.close()
        self.assertEqual(self.count(), 0)
        self.write(self.owner, "/api/transactions/2/exclusion", {"excluded": False})
        self.assertEqual(self.count(), 1)

    def test_atomic_ownership_validation_and_stale_revision(self):
        for ids, status in [
            ([1, 3], 404),
            ([999], 404),
            ([], 400),
            ([True], 400),
            (["1"], 400),
            ([0], 400),
            (list(range(1, 502)), 400),
        ]:
            self.assertEqual(
                self.write(self.owner, "/api/category-reviews", {"ids": ids}).status_code, status
            )
            self.assertEqual(self.count(), 2)
        stale = self.owner.get("/api/data").headers["X-Moa-Revision"]
        self.write(self.owner, "/api/category-reviews", {"ids": [1]})
        self.assertEqual(
            self.write(self.owner, "/api/category-reviews", {"ids": [2]}, stale).status_code, 409
        )
        self.assertEqual(self.count(), 1)
        self.assertEqual(app.test_client().get("/api/category-reviews").status_code, 401)
        self.assertEqual(
            self.owner.put("/api/category-reviews", json={"ids": [2]}).status_code, 403
        )
        self.assertEqual(
            self.owner.put("/api/category-reviews", json={"ids": [2]}, headers=HEADERS).status_code,
            428,
        )
        self.assertEqual(
            self.owner.get("/api/category-reviews").headers["Cache-Control"], "no-store"
        )

    def test_initial_migration_marks_existing_rows_once_and_cascade_keeps_no_orphans(self):
        db = connect()
        with db:
            db.execute("DROP TABLE category_reviews")
            # 버전 관리 도입 전 DB는 user_version이 0입니다. 다음 연결에서 이전 단계가 다시 실행됩니다.
            db.execute("PRAGMA user_version = 0")
        db.close()

        db = connect()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM category_reviews").fetchone()[0], 3)
        with db:
            db.execute(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(4,1,'2026-11-03','새가상상점',500)"
            )
            db.execute("DELETE FROM transactions WHERE id=1")
        db.close()
        self.assertEqual(self.count(), 1)
        db = connect()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM category_reviews").fetchone()[0], 2)
        self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        db.close()

    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_dashboard_layout_period_comparison_and_new_category_alert(self):
        """실제 임시 서버에서 1440×900·375px 화면과 신규 등록·확인 해제를 검증합니다."""
        from werkzeug.serving import make_server, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        db = connect()
        with db:
            db.executemany(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(?,?,?,?,?)",
                [
                    (5, 1, "2026-10-01", "가상이전상점", 500),
                    (6, 1, "2026-10-30", "가상말일상점", 5000),
                    (7, 1, "2026-06-01", "가상초여름상점", 700),
                ],
            )
            db.executemany(
                "INSERT INTO category_reviews(transaction_id) VALUES(?)", [(5,), (6,), (7,)]
            )
        db.close()
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                ["node", "tests/dashboard-ui.cjs"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
                check=True,
                timeout=75,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
