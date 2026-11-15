"""검증 범위: 월별 청구액·납부 여부·기본 결제일.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import tempfile
import unittest
from pathlib import Path
from app import app, connect
from auth import set_password

HEADERS = {"X-Requested-With": "CardDashboard"}


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.tmp.name) / "db.sqlite3")
        )
        set_password(connect, "fixture-login-password")
        self.client = app.test_client()
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, month="2026-10"):
        return self.client.get("/api/billing?month=" + month).json

    def save(self):
        return self.client.put(
            "/api/billing/2026-10/삼성",
            json=dict(statement_status="received", amount=1200, due_on=None, paid=False),
            headers=HEADERS,
        )

    def test_missing_known_zero_unknown_and_month_isolation(self):
        self.assertEqual(self.report()["missing_count"], 6)
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.report()["unpaid"], 1200)
        self.client.put(
            "/api/billing/2026-10/롯데",
            json=dict(statement_status="no_bill", amount=None, due_on=None, paid=False),
            headers=HEADERS,
        )
        data = self.report()
        self.assertEqual(data["missing_count"], 4)
        self.assertEqual(data["unknown_count"], 4)
        self.assertEqual(self.report("2026-11")["missing_count"], 6)
        self.assertEqual(self.report("2026-11")["total"], 0)
        self.assertEqual(self.client.get("/api/data").json["transactions"], [])

    def test_payment_is_explicit_and_reversible(self):
        self.save()
        payload = dict(statement_status="received", amount=1200, due_on="2026-10-31", paid=True)
        endpoint = "/api/billing/2026-10/삼성"
        self.assertEqual(self.client.put(endpoint, json=payload, headers=HEADERS).status_code, 200)
        self.assertEqual(self.report()["unpaid"], 0)
        payload["paid"] = False
        self.client.put(endpoint, json=payload, headers=HEADERS)
        self.assertEqual(self.report()["unpaid"], 1200)

    def test_settings_clamp_leap_day_and_keep_overrides_and_disabled_history(self):
        endpoint = "/api/billing/settings/삼성"
        self.client.put(endpoint, json=dict(enabled=True, due_day=31), headers=HEADERS)
        row = next(row for row in self.report("2028-02")["cards"] if row["card"] == "삼성")
        self.assertEqual(row["due_on"], "2028-02-29")
        self.client.put(
            "/api/billing/2028-02/삼성",
            json=dict(statement_status="received", amount=50, due_on="2028-02-27", paid=False),
            headers=HEADERS,
        )
        self.client.put(endpoint, json=dict(enabled=False, due_day=15), headers=HEADERS)
        report = self.report("2028-02")
        self.assertEqual(report["total"], 0)
        self.assertEqual(report["missing_count"], 5)
        row = next(row for row in report["cards"] if row["card"] == "삼성")
        self.assertEqual(row["due_on"], "2028-02-27")
        self.assertEqual(row["amount"], 50)
        self.client.put(endpoint, json=dict(enabled=True, due_day=15), headers=HEADERS)
        self.assertEqual(self.report("2028-02")["total"], 50)

    def test_validation_atomic_and_auth(self):
        self.save()
        base = dict(statement_status="received", amount=1200, due_on=None, paid=False)
        for patch in [
            dict(amount=-1),
            dict(amount=True),
            dict(amount=1.5),
            dict(amount=10**13),
            dict(due_on="2026-11-01"),
            dict(due_on="2026-10-32"),
            dict(paid=1),
            dict(statement_status="no_bill"),
            dict(statement_status="missing", paid=True),
            dict(amount=None, paid=True),
        ]:
            response = self.client.put(
                "/api/billing/2026-10/삼성", json={**base, **patch}, headers=HEADERS
            )
            self.assertEqual(response.status_code, 400, patch)
        self.assertEqual(self.report()["total"], 1200)
        self.assertEqual(self.client.get("/api/billing?month=2026-13").status_code, 400)
        self.assertEqual(
            self.client.put("/api/billing/2026-10/unknown", json=base, headers=HEADERS).status_code,
            400,
        )
        for day in [0, 32, True, "1.5"]:
            self.assertEqual(
                self.client.put(
                    "/api/billing/settings/삼성",
                    json=dict(enabled=True, due_day=day),
                    headers=HEADERS,
                ).status_code,
                400,
            )
        self.assertEqual(app.test_client().get("/api/billing?month=2026-10").status_code, 401)
        self.assertEqual(self.client.put("/api/billing/2026-10/삼성", json=base).status_code, 403)
