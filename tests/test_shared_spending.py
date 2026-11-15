"""사용 내역 함께 보기(기본 켜짐·거래별 숨김·메모 포함)와 소비에서 제외를 가상 자료로 검증합니다."""

import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from werkzeug.serving import WSGIRequestHandler, make_server

from app import app, connect
from auth import set_password

HEADERS = {"X-Requested-With": "CardDashboard"}
ALLOWED = {
    "used_on",
    "used_time",
    "merchant",
    "amount",
    "category",
    "card",
    "last_four",
    "payment_kind",
    "memo",
    "receipts",
}


class SharedSpendingTests(unittest.TestCase):
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
            db.execute(
                "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(11,'삼성','PRIVATE_FILE.csv','fixture-11',2)"
            )
            db.execute(
                "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(12,'현금','직접 입력 · 2026-11','manual:2026-11',2)"
            )
            db.execute(
                "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(21,'신한','OWNER_FILE.csv','fixture-21',1)"
            )
            db.executemany(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount,card_last_four) VALUES(?,?,?,?,?,?)",
                [
                    (101, 11, "2026-11-03", "가상빵집", 4500, "9876"),
                    (102, 11, "2026-11-05", "가상중복", 500, "9876"),
                    (103, 12, "2026-11-06", "가상시장", 8000, ""),
                    (104, 11, "2026-10-30", "지난달가게", 1000, "9876"),
                    (105, 11, "2026-11-07", "가상환불", -2000, "9876"),
                    (201, 21, "2026-11-04", "소유자가게", 7000, ""),
                ],
            )
            db.execute(
                "INSERT INTO transaction_annotations(transaction_id,category,actual_merchant,memo) "
                "VALUES(101,'식비','가상 동네빵집','PRIVATE_MEMO')"
            )
            db.execute("INSERT INTO transaction_exclusions(transaction_id) VALUES(102)")
        db.close()

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def share(self, client, enabled):
        revision = client.get("/api/features").headers["X-Moa-Revision"]
        response = client.put(
            "/api/features",
            json={"feature": "share_transactions", "enabled": enabled},
            headers={**HEADERS, "If-Match": revision},
        )
        self.assertEqual(response.status_code, 200)

    def detail(self, client, transaction_id, **changes):
        revision = client.get("/api/data").headers["X-Moa-Revision"]
        return client.put(
            f"/api/transactions/{transaction_id}/category",
            json={"category": None, "update_category": False, **changes},
            headers={**HEADERS, "If-Match": revision},
        )

    def test_shared_by_default_with_memo_and_allowlist(self):
        data = self.owner.get("/api/shared-transactions?month=2026-11").json
        self.assertTrue(data["sharing"])
        [member] = data["members"]
        self.assertEqual((member["username"], member["shared"]), ("partner", True))
        rows = member["transactions"]
        self.assertEqual(
            [(row["used_on"], row["merchant"], row["amount"]) for row in rows],
            [
                ("2026-11-07", "가상환불", -2000),
                ("2026-11-06", "가상시장", 8000),
                ("2026-11-03", "가상 동네빵집", 4500),
            ],
        )
        self.assertEqual((member["total"], member["count"]), (10500, 3))
        self.assertEqual((rows[2]["category"], rows[2]["memo"]), ("식비", "PRIVATE_MEMO"))
        self.assertEqual((rows[1]["card"], rows[1]["payment_kind"]), ("현금", "manual"))
        for row in rows:
            self.assertEqual(set(row), ALLOWED)
        raw = self.owner.get("/api/shared-transactions?month=2026-11").get_data(as_text=True)
        # 원본 파일명·출처 식별자·제외 거래·다른 달은 보내지 않습니다. 카드 끝 4자리는 보입니다.
        for secret in ("PRIVATE_FILE", "fixture-11", "가상중복", "지난달가게"):
            self.assertNotIn(secret, raw)
        self.assertEqual(rows[0]["last_four"], "9876")
        # 내 거래는 상대 목록(members)이 아니라 합친 목록(timeline)에만 '내 것'으로 들어갑니다.
        self.assertEqual(data["me"]["username"], "owner")
        self.assertEqual(
            [row["merchant"] for row in data["timeline"] if row["mine"]], ["소유자가게"]
        )
        self.assertTrue(self.owner.get("/api/features").json["features"]["share_transactions"])

    def test_timeline_merges_both_people_by_date_and_time(self):
        db = connect()
        with db:
            db.execute("UPDATE transactions SET used_time='09:15' WHERE id=101")
            db.execute(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount,used_time) "
                "VALUES(202,21,'2026-11-03','소유자아침',3000,'08:05')"
            )
            db.execute(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount,used_time) "
                "VALUES(203,21,'2026-11-03','소유자저녁',6000,'19:40')"
            )
        db.close()
        self.assertEqual(self.detail(self.owner, 203, shared_hidden=True).status_code, 200)
        data = self.owner.get("/api/shared-transactions?month=2026-11").json
        order = [(row["who"], row["merchant"]) for row in data["timeline"]]
        self.assertEqual(
            order,
            [
                ("partner", "가상환불"),
                ("partner", "가상시장"),
                ("owner", "소유자가게"),
                ("owner", "소유자저녁"),
                ("partner", "가상 동네빵집"),
                ("owner", "소유자아침"),
            ],
        )
        # 내 내역에서는 숨긴 거래도 보이되 표시하고, 상대 응답에는 넣지 않습니다.
        mine = {row["merchant"]: row for row in data["timeline"] if row["mine"]}
        self.assertTrue(mine["소유자저녁"]["hidden"])
        self.assertEqual((data["me"]["total"], data["me"]["count"]), (16000, 3))
        partner_view = self.partner.get("/api/shared-transactions?month=2026-11").json
        self.assertNotIn("소유자저녁", [row["merchant"] for row in partner_view["timeline"]])
        for row in partner_view["timeline"]:
            if not row["mine"]:
                self.assertEqual(set(row) - {"who", "mine"}, ALLOWED)

    def test_hidden_transaction_and_turning_off(self):
        self.assertEqual(self.detail(self.partner, 101, shared_hidden=True).status_code, 200)
        [member] = self.owner.get("/api/shared-transactions?month=2026-11").json["members"]
        self.assertEqual(
            [row["merchant"] for row in member["transactions"]], ["가상환불", "가상시장"]
        )
        # 숨김은 함께 보기에서만 빠지고, 본인 목록·메모는 그대로입니다.
        own = {row["id"]: row for row in self.partner.get("/api/data").json["transactions"]}
        self.assertEqual((own[101]["shared_hidden"], own[101]["memo"]), (1, "PRIVATE_MEMO"))
        self.assertEqual(self.detail(self.partner, 101, shared_hidden=False).status_code, 200)
        self.share(self.partner, False)
        self.assertFalse(
            self.owner.get("/api/shared-transactions?month=2026-11").json["members"][0]["shared"]
        )
        [member] = self.partner.get("/api/shared-transactions?month=2026-11").json["members"]
        self.assertEqual([row["merchant"] for row in member["transactions"]], ["소유자가게"])

    def test_not_spending_is_left_out_of_every_total_and_can_be_restored(self):
        household = lambda: self.owner.get("/api/household?month=2026-11").json["net"]
        before = household()
        self.assertEqual(self.detail(self.partner, 101, not_spending=True).status_code, 200)
        own = {row["id"]: row for row in self.partner.get("/api/data").json["transactions"]}
        self.assertEqual((own[101]["excluded"], own[101]["excluded_reason"]), (1, "not_spending"))
        self.assertEqual(household(), before - 4500)
        [member] = self.owner.get("/api/shared-transactions?month=2026-11").json["members"]
        self.assertNotIn("가상 동네빵집", [row["merchant"] for row in member["transactions"]])
        # 중복으로 제외한 거래는 '소비 아님' 해제로 되살아나지 않습니다.
        self.assertEqual(self.detail(self.partner, 102, not_spending=False).status_code, 200)
        own = {row["id"]: row for row in self.partner.get("/api/data").json["transactions"]}
        self.assertEqual((own[102]["excluded"], own[102]["excluded_reason"]), (1, "duplicate"))
        self.assertEqual(self.detail(self.partner, 101, not_spending=False).status_code, 200)
        self.assertEqual(household(), before)
        # 잘못된 값은 아무것도 저장하지 않습니다.
        response = self.detail(self.partner, 101, not_spending="yes", memo="바뀌면 안 됨")
        self.assertEqual(response.status_code, 400)
        own = {row["id"]: row for row in self.partner.get("/api/data").json["transactions"]}
        self.assertEqual(own[101]["memo"], "PRIVATE_MEMO")

    def test_bank_rows_are_decided_on_bank_screen(self):
        db = connect()
        with db:
            db.execute(
                "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(13,'우리은행','계좌','bank-expense:fixture',2)"
            )
            db.execute(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(106,13,'2026-11-08','가상이체',3000)"
            )
        db.close()
        response = self.detail(self.partner, 106, not_spending=True, memo="바뀌면 안 됨")
        self.assertEqual(response.status_code, 400)
        own = {row["id"]: row for row in self.partner.get("/api/data").json["transactions"]}
        self.assertEqual((own[106]["excluded"], own[106]["memo"]), (0, ""))

    def test_sharing_grants_no_edit_or_detail_access(self):
        revision = self.owner.get("/api/data").headers["X-Moa-Revision"]
        for method, path, body in (
            ("PUT", "/api/transactions/101/category", {"category": None, "shared_hidden": True}),
            ("PUT", "/api/transactions/101/category", {"category": None, "not_spending": True}),
            ("PUT", "/api/transactions/101/exclusion", {"excluded": True}),
            ("DELETE", "/api/transactions/manual/103", None),
            ("PUT", "/api/transactions/manual/103", {"amount": 1}),
        ):
            response = self.owner.open(
                path, method=method, json=body, headers={**HEADERS, "If-Match": revision}
            )
            self.assertIn(response.status_code, (400, 403, 404), (method, path))
        owner_rows = self.owner.get("/api/data").json["transactions"]
        self.assertNotIn(101, [row["id"] for row in owner_rows])

    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1",
        "set MOA_BROWSER_TESTS=1 for sandboxed Chromium integration",
    )
    def test_shared_view_hide_and_not_spending_in_real_browser(self):
        db = connect()
        with db:
            db.execute(
                "INSERT INTO transaction_receipts(transaction_id,mime,data) VALUES(101,'image/jpeg',?)",
                (b"\xff\xd8\xffFAKE-RECEIPT",),
            )
        db.close()

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                ["node", "tests/shared-activity-ui.cjs"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
                check=True,
                timeout=60,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_validation_and_login_required(self):
        self.assertEqual(
            app.test_client().get("/api/shared-transactions?month=2026-11").status_code, 401
        )
        self.assertEqual(self.owner.get("/api/shared-transactions?month=2026-13").status_code, 400)


if __name__ == "__main__":
    unittest.main()
