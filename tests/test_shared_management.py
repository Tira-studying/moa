"""관리 정보의 읽기 공유와 개인 원본·쓰기 권한 경계를 가상 자료로 검증합니다."""

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app import app, connect
from auth import set_password

HEADERS = {"X-Requested-With": "CardDashboard"}


class SharedManagementTests(unittest.TestCase):
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
            for number in (11, 12, 13):
                db.execute(
                    "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(?,'삼성',?,?,2)",
                    (number, f"PRIVATE_FILE_{number}.csv", f"fixture-{number}"),
                )
            db.executemany(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(?,?,?,?,?)",
                [
                    (101, 11, "2026-09-03", "가상정기B", 1000),
                    (102, 11, "2026-10-03", "가상정기B", 1000),
                    (103, 11, "2026-11-03", "가상정기B", 1200),
                    (104, 11, "2026-11-05", "가상중복B", 500),
                    (105, 12, "2026-11-05", "가상중복B", 500),
                    (106, 13, "2026-11-05", "가상중복B", 500),
                    (107, 11, "2026-11-07", "비공개일반상점", 999),
                ],
            )
            db.execute(
                "INSERT INTO transaction_annotations(transaction_id,memo) VALUES(107,'PRIVATE_MEMO')"
            )
            db.execute("INSERT INTO transaction_exclusions(transaction_id) VALUES(106)")
            db.execute(
                "INSERT INTO merchant_categories(merchant_key,category,owner_id) VALUES('공유가상분류','의료',2)"
            )
            db.execute(
                "INSERT INTO merchant_categories(merchant_key,category,owner_id) VALUES('네이버페이','쇼핑',2)"
            )
            db.execute(
                "INSERT INTO recurring_decisions(card,merchant_key,status,owner_id) VALUES('삼성','가상정기b','confirmed',2)"
            )
            db.execute(
                "INSERT INTO installment_plans(id,name,card,principal,months,first_month,paid_count,owner_id) VALUES(71,'가상노트북','삼성',900,3,'2026-11',1,2)"
            )
            db.execute(
                "INSERT INTO billing_settings(card,enabled,due_day,owner_id) VALUES('삼성',1,31,2)"
            )
            db.execute(
                "INSERT INTO monthly_bills(month,card,statement_status,amount,due_on,paid,owner_id) VALUES('2026-11','삼성','received',700,'2026-11-27',1,2)"
            )
        db.close()

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def test_both_accounts_can_read_allowed_information_without_raw_metadata(self):
        for client in (self.owner, self.partner):
            response = client.get("/api/shared-management?month=2026-11")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            data = response.json
            self.assertTrue(data["readonly"])
            self.assertEqual(data["month"], "2026-11")
            member = next(row for row in data["members"] if row["username"] == "partner")
            self.assertEqual(member["rules"], [{"merchant": "공유가상분류", "category": "의료"}])
            self.assertEqual(member["installments"]["plans"][0]["name"], "가상노트북")
            self.assertEqual(member["installments"]["remaining_principal"], 600)
            self.assertEqual(
                member["duplicates"],
                [
                    dict(
                        merchant="가상중복B",
                        card="삼성",
                        used_on="2026-11-05",
                        amount=500,
                        candidate_count=2,
                    )
                ],
            )
            self.assertEqual(len(member["excluded"]), 1)
            self.assertEqual(member["recurring"][0]["status"], "confirmed")
            self.assertEqual(member["recurring"][0]["change"], 200)
            self.assertEqual(
                member["recurring"][0]["history"][0], dict(used_on="2026-09-03", amount=1000)
            )
            bill = next(row for row in member["billing"]["cards"] if row["card"] == "삼성")
            self.assertEqual(
                (bill["amount"], bill["due_day"], bill["due_on"], bill["paid"]),
                (700, 31, "2026-11-27", True),
            )
            for private in ("PRIVATE_FILE", "PRIVATE_MEMO", "비공개일반상점"):
                self.assertNotIn(private, response.text)
            forbidden = {
                "id",
                "owner_id",
                "transaction_id",
                "import_id",
                "filename",
                "memo",
                "actual_merchant",
                "imported_at",
                "merchant_key",
            }

            def inspect(value):
                if isinstance(value, dict):
                    self.assertFalse(forbidden.intersection(value))
                    for item in value.values():
                        inspect(item)
                elif isinstance(value, list):
                    for item in value:
                        inspect(item)

            inspect(data)
        # 기존 개인 API는 여전히 본인만 반환하고 URL의 다른 소유자 번호도 무시합니다.
        self.assertEqual(
            self.owner.get("/api/finance?month=2026-11&owner_id=2").json["installments"]["plans"],
            [],
        )
        self.assertEqual(self.owner.get("/api/billing?month=2026-11&owner_id=2").json["total"], 0)
        self.assertEqual(self.owner.get("/api/data").json["transactions"], [])

    def test_sharing_does_not_grant_edit_delete_or_original_access(self):
        revision = self.owner.get("/api/data").headers["X-Moa-Revision"]
        plan = dict(
            name="변경시도",
            card="삼성",
            principal=900,
            months=3,
            first_month="2026-11",
            paid_count=2,
        )
        for path, payload, method in [
            ("/api/installments/71", plan, "PUT"),
            ("/api/installments/71", None, "DELETE"),
            ("/api/transactions/104/category", {"category": "식비"}, "PUT"),
            ("/api/transactions/104/exclusion", {"excluded": True}, "PUT"),
            ("/api/transactions/103/recurring", {"status": "ignored"}, "PUT"),
            ("/api/imports/11", None, "DELETE"),
        ]:
            result = self.owner.open(
                path, method=method, json=payload, headers={**HEADERS, "If-Match": revision}
            )
            self.assertEqual(result.status_code, 404, path)
        # 본인의 납부 기록 수정은 상대방 공유 정보에 영향을 주지 않습니다.
        self.assertEqual(
            self.owner.put(
                "/api/billing/2026-11/삼성",
                json=dict(statement_status="received", amount=100, due_on=None, paid=False),
                headers={**HEADERS, "If-Match": revision},
            ).status_code,
            200,
        )
        response = self.partner.get("/api/shared-management?month=2026-11").json
        partner = next(row for row in response["members"] if row["username"] == "partner")
        self.assertEqual(partner["billing"]["total"], 700)
        self.assertEqual(partner["installments"]["plans"][0]["name"], "가상노트북")
        self.assertEqual(
            self.owner.post(
                "/api/shared-management?month=2026-11", json={}, headers=HEADERS
            ).status_code,
            405,
        )

    def test_new_private_calculation_fields_are_not_automatically_shared(self):
        import finance

        original = finance.billing_report

        def expanded(*args, **kwargs):
            data = original(*args, **kwargs)
            data["private_note"] = "PRIVATE_FUTURE_FIELD"
            data["cards"][0]["account_number"] = "PRIVATE_ACCOUNT_FIELD"
            return data

        with patch("shared_management.finance.billing_report", side_effect=expanded):
            response = self.owner.get("/api/shared-management?month=2026-11")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("PRIVATE_", response.text)
        self.assertNotIn("account_number", response.text)

    def test_auth_validation_and_disabled_users(self):
        self.assertEqual(
            app.test_client().get("/api/shared-management?month=2026-11").status_code, 401
        )
        self.assertEqual(self.owner.get("/api/shared-management?month=2026-13").status_code, 400)
        revision = self.owner.get("/api/members").headers["X-Moa-Revision"]
        self.assertEqual(
            self.owner.put(
                "/api/members/2", json={"active": False}, headers={**HEADERS, "If-Match": revision}
            ).status_code,
            200,
        )
        self.assertEqual(
            [
                row["username"]
                for row in self.owner.get("/api/shared-management?month=2026-11").json["members"]
            ],
            ["owner"],
        )
        self.assertEqual(self.partner.get("/api/shared-management?month=2026-11").status_code, 401)
