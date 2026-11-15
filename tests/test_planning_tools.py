"""검증 범위: 연말정산 카드 공제 계산, 분야별 예산 상태, 구독·정기 결제 요약, 기능 켜기·끄기와 본인 소유 제한.

가상 급여·거래만 사용합니다.
"""

import os
import subprocess
import tempfile
import threading
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import budgets
import subscriptions
import tax_deduction as tax
from app import app, connect
from auth import set_password

HEADERS = {"X-Requested-With": "CardDashboard"}
RULES = tax.RULES[2026]


def row(**values):
    base = {
        "card": "신한",
        "merchant": "가상상점",
        "actual_merchant": "",
        "amount": 1000,
        "used_on": "2026-03-01",
        "category_key": "식비",
        "payment_kind": "card",
        "excluded": 0,
    }
    return {**base, **values}


class TaxDeductionTests(unittest.TestCase):
    def test_threshold_is_taken_from_lowest_rate_first_and_extra_limit_applies(self):
        result = tax.deduction(
            {"credit": 20_000_000, "debit": 5_000_000, "transit": 1_000_000},
            50_000_000,
            0,
            RULES,
        )
        # 문턱 1,250만원은 신용카드에서 먼저 뺍니다: 750만×15% + 500만×30% + 100만×40% = 302.5만원
        self.assertEqual(result["threshold"], 12_500_000)
        self.assertEqual(result["gross"], 3_025_000)
        # 기본한도 300만원을 넘는 2.5만원은 대중교통 공제분 안에서 추가한도로 인정합니다.
        self.assertEqual((result["basic_limit"], result["extra"]), (3_000_000, 25_000))
        self.assertEqual(result["deduction"], 3_025_000)

    def test_children_raise_basic_limit_and_high_salary_moves_culture_to_credit(self):
        spent = {"credit": 60_000_000}
        self.assertEqual(tax.deduction(spent, 50_000_000, 0, RULES)["deduction"], 3_000_000)
        self.assertEqual(tax.deduction(spent, 50_000_000, 1, RULES)["deduction"], 3_500_000)
        self.assertEqual(tax.deduction(spent, 50_000_000, 3, RULES)["deduction"], 4_000_000)
        self.assertEqual(tax.deduction(spent, 80_000_000, 0, RULES)["basic_limit"], 2_500_000)
        high = tax.deduction({"culture": 30_000_000}, 80_000_000, 0, RULES)
        self.assertEqual(high["gross"], int((30_000_000 - 20_000_000) * 0.15))

    def test_below_threshold_reports_remaining_amount(self):
        result = tax.deduction({"credit": 4_000_000}, 40_000_000, 0, RULES)
        self.assertEqual((result["to_threshold"], result["deduction"]), (6_000_000, 0))

    def test_classification_of_transactions(self):
        types = {"우리": "debit", "삼성": "mixed"}
        classify = lambda **v: tax.classify(row(**v), types, True)
        self.assertEqual(classify(), "credit")
        self.assertEqual(classify(card="우리"), "debit")
        self.assertEqual(classify(card="삼성"), "credit")
        self.assertEqual(classify(merchant="서울교통공사 지하철"), "transit")
        self.assertEqual(classify(merchant="카카오택시 버스터미널점"), "credit")
        self.assertEqual(classify(merchant="CGV 강남"), "culture")
        self.assertIsNone(classify(category_key="통신·공과금"))
        self.assertIsNone(classify(merchant="가상아파트 관리비"))
        self.assertIsNone(classify(payment_kind="bank"))
        self.assertIsNone(classify(excluded=1))
        self.assertEqual(tax.classify(row(merchant="CGV"), types, False), "credit")

    def test_report_adds_adjustments_counts_months_and_projects_year_end(self):
        settings = {
            "year": 2026,
            "salary": 40_000_000,
            "children": 0,
            "issuer_types": {},
            "adjustments": {"debit": 2_000_000},
        }
        rows = [
            row(used_on="2026-09-10", amount=1_000_000),
            row(used_on="2026-10-10", amount=3_000_000),
            row(used_on="2025-12-31", amount=9_000_000),
        ]
        result = tax.report(rows, settings, 2026, date(2026, 11, 3))
        self.assertEqual(result["moa_spent"]["credit"], 4_000_000)
        self.assertEqual(result["by_kind"]["debit"], 2_000_000)
        self.assertEqual(result["months"], ["2026-09", "2026-10"])
        self.assertEqual((result["monthly_average"], result["months_left"]), (2_000_000, 1))
        self.assertEqual(result["projection"]["spent"], 8_000_000)
        self.assertFalse(result["fallback_rules"])
        self.assertTrue(tax.report([], settings, 2031, date(2031, 1, 5))["fallback_rules"])

    def test_settings_validation(self):
        good = {
            "year": 2026,
            "salary": 50_000_000,
            "children": 1,
            "issuer_types": {"우리": "debit"},
        }
        self.assertEqual(tax.validate_settings(good, ["우리"])["children"], 1)
        for bad in (
            {**good, "salary": -1},
            {**good, "salary": True},
            {**good, "issuer_types": {"없는카드": "debit"}},
            {**good, "issuer_types": {"우리": "gift"}},
            {**good, "adjustments": {"unknown": 1}},
            {**good, "year": 1999},
        ):
            with self.assertRaises(ValueError):
                tax.validate_settings(bad, ["우리"])


class BudgetTests(unittest.TestCase):
    def test_status_over_warn_pace_ok(self):
        rows = [
            row(used_on="2026-11-02", amount=90_000),
            row(used_on="2026-11-02", amount=-10_000),
            row(used_on="2026-11-03", amount=50_000, category_key="커피"),
            row(used_on="2026-11-03", amount=30_000, category_key="교통"),
            row(used_on="2026-11-03", amount=99_000, category_key="교통", excluded=1),
            row(used_on="2026-10-30", amount=500_000),
        ]
        plan = {"식비": 100_000, "커피": 40_000, "교통": 100_000, budgets.TOTAL: 1_000_000}
        result = budgets.report(rows, plan, {}, "2026-11", date(2026, 11, 3))
        state = {item["category"]: item["status"] for item in result["items"]}
        # 식비 8만원(80%)=warn, 커피 초과=over, 교통 30%는 지난 날짜(3/30=10%)보다 빨라 pace
        self.assertEqual(
            state,
            {"식비": "warn", "커피": "over", "교통": "pace", budgets.TOTAL: "ok"},
        )
        self.assertEqual(result["items"][0]["category"], budgets.TOTAL)
        self.assertEqual(result["alerts"], 2)
        self.assertEqual(budgets.elapsed_ratio("2026-10", date(2026, 11, 3)), 1.0)
        self.assertEqual(budgets.elapsed_ratio("2026-12", date(2026, 11, 3)), 0.0)

    def test_validation_drops_zero_and_rejects_unknown(self):
        result = budgets.validate(
            {"items": [{"category": "식비", "amount": 0}, {"category": "커피", "amount": 5}]},
            {"식비", "커피"},
        )
        self.assertEqual(result, {"커피": 5})
        for bad in (
            {"items": [{"category": "없음", "amount": 1}]},
            {"items": [{"category": "식비", "amount": -1}]},
            {},
        ):
            with self.assertRaises(ValueError):
                budgets.validate(bad, {"식비"})


class SubscriptionTests(unittest.TestCase):
    def test_totals_increases_upcoming_and_ignored(self):
        recurring = [
            dict(
                merchant="가상OTT",
                card="신한",
                transaction_id=1,
                amount=17_000,
                change=2_000,
                occurrences=4,
                last_used_on="2026-10-05",
                next_expected="2026-11-05",
                status="candidate",
            ),
            dict(
                merchant="가상통신",
                card="우리",
                transaction_id=2,
                amount=50_000,
                change=0,
                occurrences=6,
                last_used_on="2026-10-25",
                next_expected="2026-11-25",
                status="confirmed",
            ),
            dict(
                merchant="가상헬스",
                card="우리",
                transaction_id=3,
                amount=60_000,
                change=0,
                occurrences=3,
                last_used_on="2026-10-04",
                next_expected="2026-11-04",
                status="ignored",
            ),
        ]
        result = subscriptions.report(
            recurring, {1: "구독료", 2: "통신·공과금"}, {"구독료": "구독"}, date(2026, 11, 3)
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual((result["monthly"], result["yearly"]), (67_000, 804_000))
        self.assertEqual(result["subscription_monthly"], 17_000)
        self.assertEqual(result["items"][0]["category"], "구독")
        self.assertEqual([item["merchant"] for item in result["increased"]], ["가상OTT"])
        self.assertEqual([item["merchant"] for item in result["upcoming"]], ["가상OTT"])


class ApiTests(unittest.TestCase):
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

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def put(self, client, path, body):
        revision = client.get("/api/features").headers["X-Moa-Revision"]
        return client.put(path, json=body, headers={**HEADERS, "If-Match": revision})

    def test_features_default_off_and_are_per_user(self):
        self.assertEqual(
            self.owner.get("/api/features").json["features"],
            {
                "card_performance": False,
                "tax_deduction": False,
                "budgets": False,
                "subscriptions": False,
                # 사용 내역 함께 보기만 기본 켜짐입니다(features.DEFAULTS).
                "share_transactions": True,
            },
        )
        for path in ("/api/tax-deduction", "/api/budgets", "/api/subscriptions"):
            self.assertEqual(self.owner.get(path).json, {"enabled": False})
        self.assertEqual(
            self.put(
                self.owner, "/api/features", {"feature": "budgets", "enabled": True}
            ).status_code,
            200,
        )
        self.assertTrue(self.owner.get("/api/budgets").json["enabled"])
        self.assertEqual(self.partner.get("/api/budgets").json, {"enabled": False})
        self.assertEqual(
            self.put(self.owner, "/api/features", {"feature": "nope", "enabled": True}).status_code,
            400,
        )

    def test_settings_are_saved_per_user(self):
        for feature in ("tax_deduction", "budgets"):
            self.put(self.owner, "/api/features", {"feature": feature, "enabled": True})
            self.put(self.partner, "/api/features", {"feature": feature, "enabled": True})
        self.assertEqual(
            self.put(
                self.owner,
                "/api/tax-deduction/settings",
                {"year": 2026, "salary": 50_000_000, "children": 2},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.owner.get("/api/tax-deduction?year=2026").json["basic_limit"], 4_000_000
        )
        self.assertEqual(
            self.partner.get("/api/tax-deduction?year=2026").json["settings"]["salary"], 0
        )
        self.assertEqual(self.owner.get("/api/tax-deduction?year=abc").status_code, 400)
        self.assertEqual(
            self.put(
                self.owner, "/api/budgets", {"items": [{"category": "식비", "amount": 300_000}]}
            ).status_code,
            200,
        )
        self.assertEqual(self.owner.get("/api/budgets").json["items"][0]["budget"], 300_000)
        self.assertEqual(self.partner.get("/api/budgets").json["items"], [])
        self.assertEqual(
            self.put(
                self.owner, "/api/budgets", {"items": [{"category": "없음", "amount": 1}]}
            ).status_code,
            400,
        )


class BrowserTests(ApiTests):
    test_features_default_off_and_are_per_user = None
    test_settings_are_saved_per_user = None

    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_planning_tools_screen(self):
        from werkzeug.serving import make_server, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        months = []
        year, month = today.year, today.month
        for _ in range(4):
            months.append(f"{year}-{month:02d}")
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        current, *earlier = months
        db = connect()
        with db:
            db.execute(
                "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(1,'신한','가상','fixture',1)"
            )
            rows = [(1, f"{current}-01", "가상식당", 80_000)]
            # 반복 결제: 지난 세 달 같은 날짜에 같은 금액 (이번 달에 아직 결제 전)
            rows += [(1, f"{m}-05", "가상OTT", 17_000) for m in earlier[:3]]
            db.executemany(
                "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(?,?,?,?)", rows
            )
            db.execute(
                "INSERT INTO transaction_annotations(transaction_id,category) "
                "SELECT id,'식비' FROM transactions WHERE merchant='가상식당'"
            )
            db.execute(
                "INSERT INTO transaction_annotations(transaction_id,category) "
                "SELECT id,'구독료' FROM transactions WHERE merchant='가상OTT'"
            )
        db.close()
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                ["node", "tests/planning-tools-ui.cjs"],
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
