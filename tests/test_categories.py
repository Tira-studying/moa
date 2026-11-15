"""검증 범위: 소비 분야 자동 분류와 직접 지정.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import tempfile
import unittest
from pathlib import Path
from app import app, connect
from auth import set_password
from categories import classify

HEADERS = {"X-Requested-With": "CardDashboard"}


class CategoryTests(unittest.TestCase):
    def test_specific_rules_and_ambiguous_merchants(self):
        for merchant, expected in [
            ("스타벅스 충북점", "커피"),
            ("가상 치과의원", "의료"),
            ("가상약국", "의료"),
            ("가상 김밥", "식비"),
            ("쿠팡이츠", "식비"),
            ("쿠팡", "쇼핑"),
            ("이마트24 가상점", "편의점"),
            ("CU 가상점", "편의점"),
            ("CUBE", "미분류"),
            ("이마트 가상점", "마트·식료품"),
            ("네이버페이", "미분류"),
            ("카페24", "미분류"),
            ("홍길동", "미분류"),
        ]:
            with self.subTest(merchant=merchant):
                self.assertEqual(classify(merchant, {})[0], expected)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.tmp.name) / "db.sqlite3")
        )
        self.client = app.test_client()
        set_password(connect, "fixture-login-password")
        db = connect()
        with db:
            db.execute(
                "INSERT INTO imports(id,card,filename,digest,created) VALUES(1,'삼성','fixture','fixture','2026-10-30')"
            )
            db.executemany(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(?,?,?,?,?)",
                [
                    (1, 1, "2026-10-01", "가상 카페", 1000),
                    (2, 1, "2026-10-02", "가상카페", -100),
                    (3, 1, "2026-10-03", "다른 카페", 500),
                ],
            )
        db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_override_applies_to_matching_past_future_refunds_and_can_be_reset(self):
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)
        before = self.client.get("/api/data").json["transactions"]
        self.assertTrue(all(row["category"] == "커피" for row in before))
        result = self.client.put(
            "/api/transactions/1/category", json={"category": "식비"}, headers=HEADERS
        )
        self.assertEqual(result.status_code, 200)
        rows = {row["id"]: row for row in self.client.get("/api/data").json["transactions"]}
        self.assertEqual(rows[1]["category"], "식비")
        self.assertEqual(rows[2]["category"], "식비")
        self.assertEqual(rows[1]["category_source"], "manual")
        self.assertEqual(rows[3]["category"], "커피")
        db = connect()
        with db:
            db.execute(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(4,1,'2026-10-04','가상 카페',800)"
            )
        db.close()
        self.assertEqual(self.client.get("/api/data").json["transactions"][0]["category"], "식비")
        self.client.put("/api/transactions/1/category", json={"category": None}, headers=HEADERS)
        self.assertEqual(self.client.get("/api/data").json["transactions"][0]["category"], "커피")
        self.assertEqual(
            [(row["id"], row["amount"]) for row in before],
            [
                (row["id"], row["amount"])
                for row in self.client.get("/api/data").json["transactions"]
                if row["id"] != 4
            ],
        )

    def test_auth_request_protection_and_invalid_changes(self):
        self.assertEqual(
            self.client.put(
                "/api/transactions/1/category", json={"category": "식비"}, headers=HEADERS
            ).status_code,
            401,
        )
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)
        self.assertEqual(
            self.client.put("/api/transactions/1/category", json={"category": "식비"}).status_code,
            403,
        )
        self.assertEqual(
            self.client.put(
                "/api/transactions/1/category",
                json={"category": "식비"},
                headers={**HEADERS, "Origin": "https://other.example"},
            ).status_code,
            403,
        )
        for payload in [{"category": "<script>"}, {}, [], {"category": 42}]:
            self.assertEqual(
                self.client.put(
                    "/api/transactions/1/category", json=payload, headers=HEADERS
                ).status_code,
                400,
            )
        self.assertEqual(
            self.client.put(
                "/api/transactions/999/category", json={"category": "식비"}, headers=HEADERS
            ).status_code,
            404,
        )
        self.assertEqual(self.client.get("/api/data").headers["Cache-Control"], "no-store")
