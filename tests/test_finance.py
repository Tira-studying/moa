"""검증 범위: 할부 예상액·중복·반복 결제 후보.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import tempfile
import unittest
from pathlib import Path

from app import app, connect
from auth import set_password
from finance import installment_report, recurring_candidates, duplicate_groups

HEADERS = {"X-Requested-With": "CardDashboard"}


def transaction(
    number, used_on, amount=100, merchant="가상 구독", card="삼성", import_id=1, excluded=0
):
    return dict(
        id=number,
        used_on=used_on,
        amount=amount,
        merchant=merchant,
        card=card,
        import_id=import_id,
        excluded=excluded,
    )


class FinanceCalculationTests(unittest.TestCase):
    def test_installment_rounding_remaining_and_year_boundary(self):
        plan = dict(
            id=1,
            name="가상 할부",
            card="삼성",
            principal=100,
            months=3,
            first_month="2026-12",
            paid_count=1,
        )
        report = installment_report([plan], "2027-01")
        self.assertEqual(report["remaining_principal"], 67)
        self.assertEqual([row["amount"] for row in report["schedule"][:2]], [33, 34])
        self.assertEqual(report["plans"][0]["end_month"], "2027-02")
        self.assertEqual(report["past_unconfirmed"], 0)
        old = installment_report([{**plan, "paid_count": 0}], "2027-01")
        self.assertEqual(old["past_unconfirmed"], 33)
        self.assertEqual(old["remaining_principal"], 100)  # Calendar passage never implies payment.
        complete = installment_report([{**plan, "paid_count": 3}], "2027-01")
        self.assertEqual(complete["remaining_principal"], 0)
        self.assertIsNone(complete["plans"][0]["next_month"])

    def test_duplicate_matches_require_different_files_card_and_signed_amount(self):
        rows = [transaction(1, "2026-10-01"), transaction(2, "2026-10-01")]
        self.assertEqual(duplicate_groups(rows), [])
        rows.append(transaction(3, "2026-10-01", import_id=2, merchant="가상구독"))
        self.assertEqual(len(duplicate_groups(rows)[0]["items"]), 3)
        rows[2]["excluded"] = 1
        self.assertEqual(duplicate_groups(rows), [])
        self.assertEqual(
            duplicate_groups([rows[0], transaction(4, "2026-10-01", -100, import_id=2)]), []
        )
        self.assertEqual(
            duplicate_groups([rows[0], transaction(4, "2026-10-01", card="우리", import_id=2)]), []
        )

    def test_recurring_price_change_and_end_of_month_prediction(self):
        rows = [
            transaction(1, "2026-12-31", 1000),
            transaction(2, "2027-01-31", 1000),
            transaction(3, "2027-02-28", 1200),
        ]
        result = recurring_candidates(rows, "2027-02", {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["change"], 200)
        self.assertEqual(result[0]["next_expected"], "2027-03-28")
        self.assertEqual(result[0]["occurrences"], 3)
        self.assertEqual(
            recurring_candidates(rows, "2027-05", {}), []
        )  # Old subscriptions are not forecast indefinitely.
        self.assertEqual(
            recurring_candidates(rows, "2027-02", {("삼성", "가상구독"): "ignored"})[0]["status"],
            "ignored",
        )

    def test_recurring_rejects_frequent_merchants_gaps_and_refunds(self):
        rows = [
            transaction(1, "2026-08-05"),
            transaction(2, "2026-09-05"),
            transaction(3, "2026-10-05"),
        ]
        self.assertEqual(len(recurring_candidates(rows, "2026-10", {})), 1)
        self.assertEqual(
            recurring_candidates(rows + [transaction(4, "2026-10-06")], "2026-10", {}), []
        )
        self.assertEqual(
            recurring_candidates(rows + [transaction(4, "2026-10-06", -100)], "2026-10", {}), []
        )
        self.assertEqual(recurring_candidates([rows[0], rows[2]], "2026-10", {}), [])
        self.assertEqual(
            recurring_candidates(rows + [transaction(4, "2026-10-06", excluded=1)], "2026-10", {})[
                0
            ]["occurrences"],
            3,
        )


class FinanceApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.tmp.name) / "db.sqlite3")
        )
        self.client = app.test_client()
        set_password(connect, "fixture-login-password")
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)
        db = connect()
        with db:
            db.executemany(
                "INSERT INTO imports(id,card,filename,digest) VALUES(?,?,?,?)",
                [
                    (1, "삼성", "email-fixture.html", "first"),
                    (2, "삼성", "manual-fixture.csv", "second"),
                ],
            )
            db.executemany(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(?,?,?,?,?)",
                [
                    (1, 1, "2026-10-05", "가상구독", 1200),
                    (2, 2, "2026-10-05", "가상 구독", 1200),
                    (3, 1, "2026-09-05", "가상구독", 1000),
                    (4, 1, "2026-08-05", "가상구독", 1000),
                ],
            )
        db.close()
        self.plan = dict(
            name="가상 노트북",
            card="우리",
            principal=100,
            months=3,
            first_month="2026-10",
            paid_count=0,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def report(self):
        return self.client.get("/api/finance?month=2026-10").json

    def test_installment_crud_and_validation_do_not_write_ledger(self):
        before = self.client.get("/api/data").json["transactions"]
        saved = self.client.post("/api/installments", json=self.plan, headers=HEADERS)
        self.assertEqual(saved.status_code, 200)
        plan_id = saved.json["id"]
        self.assertEqual(self.report()["installments"]["this_month"], 33)
        self.client.put(
            f"/api/installments/{plan_id}", json={**self.plan, "paid_count": 1}, headers=HEADERS
        )
        self.assertEqual(self.report()["installments"]["remaining_principal"], 67)
        for bad in [
            {"months": 0},
            {"months": True},
            {"paid_count": 4},
            {"principal": -1},
            {"first_month": "2026-99"},
            {"name": ""},
        ]:
            self.assertEqual(
                self.client.post(
                    "/api/installments", json={**self.plan, **bad}, headers=HEADERS
                ).status_code,
                400,
            )
        self.assertEqual(self.client.get("/api/data").json["transactions"], before)
        self.assertEqual(
            self.client.delete(f"/api/installments/{plan_id}", headers=HEADERS).status_code, 200
        )
        self.assertEqual(self.report()["installments"]["plans"], [])

    def test_exclusion_is_reversible_keeps_source_and_changes_recurring_detection(self):
        self.assertEqual(len(self.report()["duplicates"]), 1)
        self.assertEqual(self.report()["recurring"], [])
        self.assertEqual(
            self.client.put(
                "/api/transactions/2/exclusion", json={"excluded": True}, headers=HEADERS
            ).status_code,
            200,
        )
        data = self.client.get("/api/data").json
        self.assertEqual(len(data["transactions"]), 4)
        self.assertEqual(
            sum(row["amount"] for row in data["transactions"] if not row["excluded"]), 3200
        )
        report = self.report()
        self.assertEqual(report["duplicates"], [])
        self.assertEqual(report["recurring"][0]["change"], 200)
        self.assertEqual(
            self.client.put(
                "/api/transactions/1/exclusion", json={"excluded": True}, headers=HEADERS
            ).status_code,
            409,
        )
        self.client.put("/api/transactions/2/exclusion", json={"excluded": False}, headers=HEADERS)
        self.assertEqual(len(self.report()["duplicates"]), 1)
        self.client.put("/api/transactions/2/exclusion", json={"excluded": True}, headers=HEADERS)
        self.client.delete("/api/imports/2", headers=HEADERS)
        self.assertEqual(self.report()["excluded"], [])  # FK cleanup, no orphaned exclusions.

    def test_recurring_confirmation_ignore_and_reset(self):
        self.client.put("/api/transactions/2/exclusion", json={"excluded": True}, headers=HEADERS)
        for status in ["confirmed", "ignored", None]:
            self.assertEqual(
                self.client.put(
                    "/api/transactions/1/recurring", json={"status": status}, headers=HEADERS
                ).status_code,
                200,
            )
            self.assertEqual(self.report()["recurring"][0]["status"], status or "candidate")

    def test_auth_csrf_and_bad_requests(self):
        self.assertEqual(self.client.post("/api/installments", json=self.plan).status_code, 403)
        self.assertEqual(
            self.client.put(
                "/api/transactions/1/exclusion",
                json={"excluded": True},
                headers={**HEADERS, "Origin": "https://evil.example"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.put(
                "/api/transactions/1/exclusion", json={"excluded": 1}, headers=HEADERS
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.put(
                "/api/transactions/999/exclusion", json={"excluded": False}, headers=HEADERS
            ).status_code,
            404,
        )
        self.assertEqual(self.client.get("/api/finance?month=bad").status_code, 400)
        self.assertEqual(
            self.client.get("/api/finance?month=2026-10").headers["Cache-Control"], "no-store"
        )
        self.client.post("/api/logout", headers=HEADERS)
        self.assertEqual(self.client.get("/api/finance?month=2026-10").status_code, 401)

    def test_import_warns_about_overlap_in_another_file(self):
        import io

        def save_file(content):
            preview = self.client.post(
                "/api/preview", data={"file": (io.BytesIO(content), "fixture.csv")}, headers=HEADERS
            ).json
            return self.client.post(
                "/api/import",
                json={
                    "token": preview["token"],
                    "card": "우리",
                    "date": 0,
                    "merchant": 1,
                    "amount": 2,
                },
                headers=HEADERS,
            )

        first = save_file(b"date,merchant,amount\n2026-10-10,fixture,1000\n")
        second = save_file(b"date,merchant,amount,note\n2026-10-10,fixture,1000,second\n")
        self.assertEqual(first.json["duplicate_count"], 0)
        self.assertEqual(second.json["duplicate_count"], 1)
        self.assertEqual(second.json["count"], 1)  # Saved for review, not silently removed.
