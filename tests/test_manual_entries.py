"""검증 범위: 직접 입력 지출(현금·계좌이체 등)의 추가·수정·삭제, 입력 검사, 본인 소유 제한,
결제 수단 구분(payment_kind='manual')과 연말정산 현금영수증 반영, 화면 동작.

가상 사용처·금액과 임시 DB만 사용합니다.
"""

import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import tax_deduction as tax
from app import app, connect
from auth import set_password

HEADERS = {"X-Requested-With": "CardDashboard"}


def entry(**values):
    return {
        "used_on": "2026-11-03",
        "merchant": "가상 반찬가게",
        "amount": 12_000,
        "method": "cash",
        "category": "식비",
        "memo": "",
        "cash_receipt": False,
        **values,
    }


class ManualEntryTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.directory.name) / "db.sqlite3")
        )
        set_password(connect, "fixture-owner-password")
        self.owner = app.test_client()
        self.owner.post("/api/login", json={"password": "fixture-owner-password"}, headers=HEADERS)
        self.owner.post(
            "/api/members",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers={**HEADERS, "If-Match": self.revision(self.owner, "/api/members")},
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

    def revision(self, client, path="/api/data"):
        return client.get(path).headers["X-Moa-Revision"]

    def send(self, client, method, path, body=None):
        return client.open(
            path,
            method=method,
            json=body,
            headers={**HEADERS, "If-Match": self.revision(client)},
        )

    def rows(self, client):
        return client.get("/api/data").get_json()["transactions"]

    def imports(self, client):
        return client.get("/api/data").get_json()["imports"]

    def test_create_appears_as_manual_payment_with_category_confirmed(self):
        response = self.send(self.owner, "POST", "/api/transactions/manual", entry(memo="장보기"))
        self.assertEqual(response.status_code, 201)
        [row] = self.rows(self.owner)
        self.assertEqual(row["id"], response.get_json()["id"])
        self.assertEqual(
            (row["payment_kind"], row["card"], row["manual_method"], row["category"]),
            ("manual", "현금", "cash", "식비"),
        )
        self.assertEqual((row["amount"], row["memo"], row["cash_receipt"]), (12_000, "장보기", 0))
        self.assertIs(row["category_reviewed"], True)
        [source] = self.imports(self.owner)
        self.assertEqual(
            (source["filename"], source["payment_kind"]), ("직접 입력 · 2026-11", "manual")
        )
        # 같은 달·같은 결제 수단은 한 저장 기록에 묶입니다.
        self.send(self.owner, "POST", "/api/transactions/manual", entry(used_on="2026-11-20"))
        self.assertEqual(len(self.imports(self.owner)), 1)
        self.assertEqual(self.imports(self.owner)[0]["count"], 2)

    def test_validation(self):
        for body, message in [
            (entry(amount=0), "금액"),
            (entry(amount=True), "금액"),
            (entry(amount="12000"), "금액"),
            (entry(used_on="2026-02-30"), "날짜"),
            (entry(used_on="20261103"), "날짜"),
            (entry(method="card"), "결제 수단"),
            (entry(category="없는 분야"), "분야"),
            (entry(merchant="  "), "사용처를 입력"),
            (entry(merchant="가" * 101), "100자"),
            (entry(merchant="가상\x00상점"), "제어 문자"),
            (entry(memo="가" * 501), "500자"),
            (entry(cash_receipt="yes"), "현금영수증"),
            ([], "입력 값"),
        ]:
            with self.subTest(body=body):
                response = self.send(self.owner, "POST", "/api/transactions/manual", body)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.get_json()["error"])
        self.assertEqual(self.rows(self.owner), [])

    def test_receipt_only_for_cash_and_transfer(self):
        self.send(self.owner, "POST", "/api/transactions/manual", entry(cash_receipt=True))
        self.send(
            self.owner,
            "POST",
            "/api/transactions/manual",
            entry(method="other", cash_receipt=True, merchant="가상 상품권"),
        )
        for method, merchant in (("local", "가상 동네빵집"), ("onnuri", "가상 전통시장")):
            self.send(
                self.owner,
                "POST",
                "/api/transactions/manual",
                entry(method=method, cash_receipt=True, merchant=merchant),
            )
        receipts = {row["card"]: row["cash_receipt"] for row in self.rows(self.owner)}
        self.assertEqual(receipts, {"현금": 1, "기타 결제": 0, "지역화폐": 0, "온누리상품권": 0})
        methods = {row["merchant"]: row["manual_method"] for row in self.rows(self.owner)}
        self.assertEqual(methods["가상 전통시장"], "onnuri")

    def test_update_moves_between_records_and_removes_empty_one(self):
        created = self.send(self.owner, "POST", "/api/transactions/manual", entry())
        transaction_id = created.get_json()["id"]
        response = self.send(
            self.owner,
            "PUT",
            f"/api/transactions/manual/{transaction_id}",
            entry(used_on="2026-10-31", method="transfer", amount=50_000, category="교통"),
        )
        self.assertEqual(response.status_code, 200)
        [row] = self.rows(self.owner)
        self.assertEqual(
            (row["used_on"], row["card"], row["amount"], row["category"]),
            ("2026-10-31", "계좌이체", 50_000, "교통"),
        )
        self.assertEqual(
            [item["filename"] for item in self.imports(self.owner)], ["직접 입력 · 2026-10"]
        )

    def test_delete_removes_row_and_empty_record(self):
        transaction_id = self.send(
            self.owner, "POST", "/api/transactions/manual", entry()
        ).get_json()["id"]
        response = self.send(self.owner, "DELETE", f"/api/transactions/manual/{transaction_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((self.rows(self.owner), self.imports(self.owner)), ([], []))
        response = self.send(self.owner, "DELETE", f"/api/transactions/manual/{transaction_id}")
        self.assertEqual(response.status_code, 404)

    def test_only_own_manual_entries_can_be_changed(self):
        transaction_id = self.send(
            self.owner, "POST", "/api/transactions/manual", entry()
        ).get_json()["id"]
        for method, body in (("PUT", entry(amount=1)), ("DELETE", None)):
            response = self.send(
                self.partner, method, f"/api/transactions/manual/{transaction_id}", body
            )
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.rows(self.owner)[0]["amount"], 12_000)
        self.assertEqual(self.rows(self.partner), [])
        # 카드 명세서 거래는 직접 입력 API로 고치거나 지울 수 없습니다.
        db = connect()
        with db:
            source = db.execute(
                "INSERT INTO imports(card,filename,digest,owner_id) VALUES('신한','가상','f',1)"
            ).lastrowid
            card_row = db.execute(
                "INSERT INTO transactions(import_id,used_on,merchant,amount) "
                "VALUES(?,'2026-11-01','가상카드',1000)",
                (source,),
            ).lastrowid
        db.close()
        response = self.send(self.owner, "DELETE", f"/api/transactions/manual/{card_row}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.rows(self.owner)), 2)

    def test_tax_counts_manual_only_with_cash_receipt(self):
        def classify(**values):
            row = {
                "card": "현금",
                "merchant": "가상 반찬가게",
                "actual_merchant": "",
                "amount": 1000,
                "used_on": "2026-11-03",
                "payment_kind": "manual",
                **values,
            }
            return tax.classify(row, {}, True)

        self.assertIsNone(classify(cash_receipt=0))
        self.assertEqual(classify(cash_receipt=1), "debit")
        self.assertEqual(classify(cash_receipt=1, merchant="가상 지하철"), "transit")
        self.assertEqual(classify(payment_kind="card", card="신한"), "credit")


class ManualEntryBrowserTests(unittest.TestCase):
    setUp = ManualEntryTests.setUp
    tearDown = ManualEntryTests.tearDown
    revision = ManualEntryTests.revision

    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_manual_entry_screen(self):
        from werkzeug.serving import make_server, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                ["node", "tests/manual-entry-ui.cjs"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
                check=True,
                timeout=90,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
