"""검증 범위: 해외 결제의 원화 금액 확정(foreign_payments.py, PUT /api/transactions/<id>/foreign-amount).

적용 환율·원화 결제액 입력 검사, 원 단위 반올림, 본인 거래만 수정, 사용 내역 응답의 외화 표시를 확인합니다.
가상 가맹점·금액과 임시 DB만 사용합니다.
"""

import tempfile
import unittest
from pathlib import Path

import foreign_payments
from app import app, connect
from auth import set_password

HEADERS = {"X-Requested-With": "CardDashboard"}


class ForeignAmountRuleTests(unittest.TestCase):
    def test_rate_or_krw_gives_won_amount(self):
        self.assertEqual(foreign_payments.krw_from({"rate": "1385.5"}, "12.34"), (17097, "1385.5"))
        self.assertEqual(foreign_payments.krw_from({"krw": "17,090"}, "12.34"), (17090, ""))
        self.assertEqual(foreign_payments.krw_from({"krw": 17090}, "12.34"), (17090, ""))

    def test_invalid_input_is_rejected(self):
        for payload in (
            None,
            {},
            {"rate": "1385", "krw": "17000"},
            {"rate": "abc"},
            {"rate": "-1"},
            {"krw": "12.5"},
            {"krw": "0"},
            {"krw": True},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                foreign_payments.krw_from(payload, "12.34")

    def test_clean_foreign_amount(self):
        self.assertEqual(foreign_payments.clean_foreign_amount("1,234.50"), "1234.50")
        for value in ("0", "0.00", "-1.00", "1.2.3", None, "12.34달러"):
            self.assertIsNone(foreign_payments.clean_foreign_amount(value))


class ForeignAmountApiTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.directory.name) / "db.sqlite3")
        )
        set_password(connect, "fixture-owner-password")
        self.owner = self.login({"password": "fixture-owner-password"})
        self.owner.post(
            "/api/members",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers={**HEADERS, "If-Match": self.revision(self.owner, "/api/members")},
        )
        self.partner = self.login({"username": "partner", "password": "fixture-partner-password"})
        db = connect()
        try:
            with db:
                source = db.execute(
                    "INSERT INTO imports(card,filename,digest,owner_id) VALUES('삼성','CODEF 조회 · 가상','codef:samsung:fixture',1)"
                ).lastrowid
                self.transaction = db.execute(
                    "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(?,?,?,0)",
                    (source, "2026-11-18", "가상 해외 상점"),
                ).lastrowid
                foreign_payments.record(db, self.transaction, "USD", "12.34")
        finally:
            db.close()

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def login(self, body):
        client = app.test_client()
        client.post("/api/login", json=body, headers=HEADERS)
        return client

    def revision(self, client, path="/api/data"):
        return client.get(path).headers["X-Moa-Revision"]

    def confirm(self, client, body):
        return client.put(
            f"/api/transactions/{self.transaction}/foreign-amount",
            json=body,
            headers={**HEADERS, "If-Match": self.revision(client)},
        )

    def row(self):
        [row] = self.owner.get("/api/data").get_json()["transactions"]
        return row

    def test_pending_row_shows_foreign_amount_then_confirmed_won(self):
        row = self.row()
        self.assertEqual(
            (
                row["amount"],
                row["foreign_currency"],
                row["foreign_amount"],
                row["foreign_confirmed"],
            ),
            (0, "USD", "12.34", 0),
        )
        response = self.confirm(self.owner, {"rate": "1385.5"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["amount"], 17097)
        row = self.row()
        self.assertEqual(
            (row["amount"], row["foreign_rate"], row["foreign_confirmed"]), (17097, "1385.5", 1)
        )
        # 정확한 청구액을 알게 되면 원화 결제액으로 다시 고칠 수 있습니다.
        self.assertEqual(self.confirm(self.owner, {"krw": "17090"}).status_code, 200)
        self.assertEqual((self.row()["amount"], self.row()["foreign_rate"]), (17090, ""))

    def test_partner_cannot_confirm_owner_foreign_payment(self):
        response = self.confirm(self.partner, {"krw": "17090"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.row()["amount"], 0)

    def test_invalid_input_keeps_zero_won(self):
        self.assertEqual(self.confirm(self.owner, {"rate": "abc"}).status_code, 400)
        self.assertEqual(self.row()["foreign_confirmed"], 0)

    def test_domestic_rows_report_no_foreign_currency(self):
        db = connect()
        try:
            with db:
                db.execute("DELETE FROM foreign_amounts")
        finally:
            db.close()
        row = self.row()
        self.assertEqual((row["foreign_currency"], row["foreign_confirmed"]), ("", 1))
