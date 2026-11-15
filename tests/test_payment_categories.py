"""검증 범위: 간편결제의 거래별 분류와 실제 가맹점.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import io
import tempfile
import unittest
from pathlib import Path

from app import app, connect
from auth import set_password
from categories import classify, payment_service
from finance import recurring_candidates

HEADERS = {"X-Requested-With": "CardDashboard"}


class PaymentDetectionTests(unittest.TestCase):
    def test_known_services_variants_and_latin_boundaries(self):
        names = {
            "네이버 페이": "네이버페이",
            "네이버파이낸셜(주)": "네이버페이",
            "N_PAY123": "네이버페이",
            "NAVER PAY_스타벅스": "네이버페이",
            "카카오페이": "카카오페이",
            "KakaoPay": "카카오페이",
            "토스페이먼츠": "토스페이",
            "TOSS PAY": "토스페이",
            "PAYCO": "페이코",
            "삼성월렛": "삼성페이",
            "Apple Pay": "애플페이",
            "GooglePay": "구글페이",
            "쓱페이": "SSG페이",
            "SSG PAY": "SSG페이",
            "스마일페이": "스마일페이",
            "쿠페이": "쿠페이",
            "L.PAY": "엘페이",
            "KB PAY": "KB페이",
            "신한SOL페이": "신한페이",
            "하나페이": "하나페이",
            "우리WON페이": "우리WON페이",
            "Paybooc": "페이북",
            "제로페이": "제로페이",
            "Alipay": "알리페이",
            "WeChat Pay": "위챗페이",
            "PayPal": "페이팔",
            "Amazon Pay": "아마존페이",
            "NHN KCP": "NHN KCP",
            "KG INICIS": "KG이니시스",
            "KG모빌리언스": "KG모빌리언스",
            "NICEPAY": "나이스페이",
            "DANAL": "다날",
            "KSNET": "KSNET",
            "페이레터": "페이레터",
        }
        for name, service in names.items():
            with self.subTest(name=name):
                self.assertEqual(payment_service(name), service)
        for name in ["OpenPay", "companyNPay", "삼성생명", "네이버클라우드", "가상 카페"]:
            with self.subTest(name=name):
                self.assertIsNone(payment_service(name))
        self.assertEqual(classify("네이버페이", {"네이버페이": "식비"}), ("미분류", "unclassified"))
        self.assertEqual(classify("카카오페이_스타벅스", {}), ("커피", "rule"))
        self.assertEqual(classify("네이버페이_카페24", {})[0], "미분류")

    def test_wallet_only_names_do_not_become_recurring_merchants(self):
        rows = [
            dict(id=i, card="삼성", merchant="네이버페이", amount=1000, used_on=f"2026-{month}-05")
            for i, month in enumerate(["08", "09", "10"], 1)
        ]
        self.assertEqual(recurring_candidates(rows, "2026-10", {}), [])
        real = [{**row, "actual_merchant": "넷플릭스"} for row in rows]
        report = recurring_candidates(real, "2026-10", {})
        self.assertEqual(report[0]["merchant"], "넷플릭스")


class PaymentCategoryApiTests(unittest.TestCase):
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
            db.execute(
                "INSERT INTO imports(id,card,filename,digest) VALUES(1,'삼성','fixture.csv','fixture')"
            )
            db.executemany(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(?,?,?,?,?)",
                [
                    (1, 1, "2026-10-01", "네이버페이", 1000),
                    (2, 1, "2026-10-02", "네이버페이", 2000),
                    (3, 1, "2026-11-01", "네이버페이", 3000),
                    (4, 1, "2026-10-03", "카카오페이", 4000),
                    (5, 1, "2026-10-04", "토스페이", 5000),
                    (6, 1, "2026-10-05", "페이코", 6000),
                    (7, 1, "2026-10-06", "스타벅스", 7000),
                    (8, 1, "2026-10-07", "스타벅스", 8000),
                ],
            )
            db.execute(
                "INSERT INTO merchant_categories(merchant_key,category) VALUES('네이버페이','쇼핑')"
            )
        db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        return {row["id"]: row for row in self.client.get("/api/data").json["transactions"]}

    def edit(self, number, **payload):
        return self.client.put(
            f"/api/transactions/{number}/category", json=payload, headers=HEADERS
        )

    def test_wallet_default_is_transaction_and_old_global_rules_are_ignored(self):
        before = self.rows()
        self.assertEqual(before[1]["category"], "미분류")
        self.assertEqual(before[1]["category_scope"], "transaction")
        self.assertEqual(self.edit(1, category="식비").status_code, 200)
        rows = self.rows()
        self.assertEqual(rows[1]["category"], "식비")
        self.assertEqual(rows[1]["category_source"], "transaction")
        self.assertTrue(all(rows[number]["category"] == "미분류" for number in [2, 3]))
        db = connect()
        with db:
            db.execute(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(9,1,'2026-12-01','네이버페이',9000)"
            )
        db.close()
        self.assertEqual(self.rows()[9]["category"], "미분류")
        for number in before:
            self.assertEqual(
                tuple(before[number][key] for key in ("merchant", "amount", "used_on")),
                tuple(rows[number][key] for key in ("merchant", "amount", "used_on")),
            )
        for number in [1, 4, 5, 6]:
            self.assertEqual(self.edit(number, category="커피", scope="merchant").status_code, 400)

    def test_actual_merchant_inference_notes_and_override_priority(self):
        self.assertEqual(
            self.edit(
                2, category=None, update_category=False, actual_merchant="가상 약국", memo="처방약"
            ).status_code,
            200,
        )
        self.assertEqual(self.rows()[2]["category"], "의료")
        self.edit(2, category="식비")
        self.edit(2, category=None, update_category=False, actual_merchant="가상 카페")
        self.assertEqual(self.rows()[2]["category"], "식비")
        self.edit(2, category=None)
        self.assertEqual(self.rows()[2]["category"], "커피")
        self.assertEqual(self.rows()[2]["memo"], "처방약")
        self.edit(7, category="쇼핑")
        self.edit(3, category=None, update_category=False, actual_merchant="스타벅스")
        self.assertEqual(self.rows()[3]["category"], "쇼핑")
        self.edit(3, category="의료")
        self.assertEqual(self.rows()[3]["category"], "의료")
        self.assertEqual(self.rows()[8]["category"], "쇼핑")

    def test_ordinary_merchant_defaults_and_explicit_single_transaction_overrides(self):
        self.edit(7, category="식비")
        self.assertEqual(self.rows()[8]["category"], "식비")
        self.edit(7, category="의료", scope="transaction")
        self.edit(8, category="커피")
        self.assertEqual(self.rows()[7]["category"], "의료")
        self.assertEqual(self.rows()[8]["category"], "커피")
        self.edit(7, category=None)
        self.assertEqual(self.rows()[7]["category"], "커피")
        db = connect()
        with db:
            db.execute(
                "INSERT INTO merchant_categories(merchant_key,category) VALUES('가상약국','기타')"
            )
        db.close()
        self.edit(
            8, category=None, scope="merchant", update_category=False, actual_merchant="가상약국"
        )
        self.assertEqual(
            self.rows()[8]["category"], "기타"
        )  # Notes-only updates never delete a shared category.

    def test_invalid_metadata_is_atomic_and_annotations_follow_transaction_deletion(self):
        self.edit(1, category="식비", actual_merchant="가상식당", memo="점심")
        self.assertEqual(self.edit(1, category="의료", actual_merchant="x" * 501).status_code, 400)
        self.assertEqual(
            self.edit(
                1, category="의료", scope="merchant", actual_merchant="네이버페이"
            ).status_code,
            400,
        )
        self.assertEqual(self.rows()[1]["actual_merchant"], "가상식당")
        self.assertEqual(self.rows()[1]["category"], "식비")
        self.assertEqual(self.edit(1, category="의료", update_category="no").status_code, 400)
        self.client.delete("/api/imports/1", headers=HEADERS)
        db = connect()
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM transaction_annotations").fetchone()[0], 0
        )
        db.close()

    def test_import_optional_actual_merchant_column(self):
        content = "이용일,가맹점,금액,실제 가맹점\n2026-10-11,네이버페이,1000,가상약국\n2026-10-12,네이버페이,2000,가상카페\n".encode()
        preview = self.client.post(
            "/api/preview", data={"file": (io.BytesIO(content), "new-fixture.csv")}, headers=HEADERS
        ).json
        payload = dict(
            token=preview["token"], card="삼성", date=0, merchant=1, amount=2, actual_merchant=3
        )
        self.assertEqual(
            self.client.post(
                "/api/import", json={**payload, "actual_merchant": 1}, headers=HEADERS
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/api/import", json=payload, headers=HEADERS).status_code, 200
        )
        added = [row for row in self.rows().values() if row["id"] > 8]
        self.assertEqual({row["category"] for row in added}, {"의료", "커피"})
        self.assertTrue(all(row["merchant"] == "네이버페이" for row in added))

    def test_explicit_shared_rule_is_limited_to_a_real_merchant(self):
        self.assertEqual(
            self.edit(
                1, category="식비", scope="merchant", actual_merchant="가상 식당"
            ).status_code,
            200,
        )
        self.edit(2, category=None, update_category=False, actual_merchant="가상 식당")
        rows = self.rows()
        self.assertEqual(rows[1]["category"], "식비")
        self.assertEqual(rows[2]["category"], "식비")
        self.assertEqual(rows[3]["category"], "미분류")
        self.assertEqual(rows[1]["category_scope"], "transaction")
        db = connect()
        self.assertEqual(
            db.execute(
                "SELECT category FROM merchant_categories WHERE merchant_key='네이버페이'"
            ).fetchone()[0],
            "쇼핑",
        )
        db.close()

    def test_actual_merchant_recurring_confirmation_uses_the_real_merchant_key(self):
        db = connect()
        with db:
            db.executemany(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(?,?,?,?,?)",
                [
                    (9, 1, "2026-08-05", "카카오페이", 1000),
                    (10, 1, "2026-09-05", "카카오페이", 1000),
                    (11, 1, "2026-10-05", "카카오페이", 1000),
                ],
            )
        db.close()
        for number in [9, 10, 11]:
            self.edit(number, category=None, update_category=False, actual_merchant="넷플릭스")
        report = self.client.get("/api/finance?month=2026-10").json
        self.assertEqual(report["recurring"][0]["merchant"], "넷플릭스")
        self.client.put(
            "/api/transactions/11/recurring", json={"status": "confirmed"}, headers=HEADERS
        )
        self.assertEqual(
            self.client.get("/api/finance?month=2026-10").json["recurring"][0]["status"],
            "confirmed",
        )
