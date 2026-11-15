"""검증 범위: 우리카드의 구분 행·소계 정리.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app, connect, previews, secure_tables
from auth import set_password
from secure_html import normalize_statement_tables

HEADERS = {"X-Requested-With": "CardDashboard"}
COLUMNS = ["이용일자", "이용가맹점", "이용금액", "청구금액"]
ROWS = [
    COLUMNS,
    ["(M123)카드의정석 가상카드"] * 4,
    ["09/10", "가상 가맹점 A", "30000", "10000"],
    ["소계(가상회원)", "소계(가상회원)", "30000", "10000"],
    ["(M456)카드의정석 다른가상카드"] * 4,
    COLUMNS,
    ["09/11", "소계상사", "-3000", "-1000"],
    ["청구합계-우리은행 000****0000"] * 4,
]
DATA = b"<html>synthetic-woori</html>"


class WooriRowTests(unittest.TestCase):
    def test_only_explicit_card_summary_and_repeated_headers_are_removed(self):
        original = copy.deepcopy(ROWS)
        result = normalize_statement_tables([ROWS], "우리")
        self.assertEqual(result, [[COLUMNS, ROWS[2], ROWS[6]]])
        self.assertEqual(ROWS, original)
        self.assertEqual(normalize_statement_tables(result, "우리"), result)
        other = [ROWS]
        self.assertIs(normalize_statement_tables(other, "삼성"), other)
        unknown = [["열 1", "열 2", "열 3"], ["(M123)카드 가상"] * 3]
        self.assertEqual(normalize_statement_tables([unknown], "우리"), [unknown])

    def test_malformed_and_missing_dates_are_not_silently_discarded(self):
        bad = [["99/99", "가상 잘못된 거래", "100", "100"], ["", "가상 날짜누락", "200", "200"]]
        result = normalize_statement_tables([ROWS + bad], "우리")[0]
        self.assertEqual(result[-2:], bad)


class WooriImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.tmp.name) / "db.sqlite3")
        )
        self.client = app.test_client()
        set_password(connect, "fixture-login-password")
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)
        previews.clear()
        secure_tables.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, rows=ROWS, year="2026"):
        with patch("app.unlock_statement", return_value=[rows]):
            result = self.client.post(
                "/api/secure-html",
                data={
                    "card": "우리",
                    "file": (io.BytesIO(DATA), "fixture.html"),
                    "password": "fixture-only",
                    "year": year,
                },
                headers=HEADERS,
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["tables"][0]["suggested_header"], 1)
        return self.client.post(
            "/api/secure-preview",
            json={
                "token": result.json["token"],
                "table": 0,
                "header": 1,
                "end": result.json["tables"][0]["count"],
            },
            headers=HEADERS,
        ).json

    def save(self, preview):
        return self.client.post(
            "/api/import",
            json={"token": preview["token"], "card": "우리", "date": 0, "merchant": 1, "amount": 2},
            headers=HEADERS,
        )

    def test_clean_preview_imports_all_sections_and_keeps_selected_amounts(self):
        preview = self.prepare()
        self.assertEqual(preview["count"], 2)
        self.assertEqual(self.save(preview).json["count"], 2)
        rows = self.client.get("/api/data").json["transactions"]
        self.assertEqual({row["used_on"] for row in rows}, {"2026-09-10", "2026-09-11"})
        self.assertEqual(sum(row["amount"] for row in rows), 27000)
        self.assertEqual(self.save(self.prepare()).status_code, 409)

    def test_normalization_preserves_old_table_duplicate_fingerprint(self):
        digest = (
            hashlib.sha256(DATA).hexdigest()
            + ":"
            + hashlib.sha256(
                json.dumps(ROWS, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
        )
        db = connect()
        with db:
            cursor = db.execute(
                "INSERT INTO imports(card,filename,digest) VALUES(?,?,?)",
                ("우리", "old-fixture.html", digest),
            )
            db.execute(
                "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(?,?,?,?)",
                (cursor.lastrowid, "2026-09-10", "가상 가맹점 A", 30000),
            )
        db.close()
        self.assertEqual(self.save(self.prepare()).status_code, 409)
        self.assertEqual(len(self.client.get("/api/data").json["transactions"]), 1)

    def test_invalid_transaction_blocks_entire_save_and_year_is_still_required(self):
        preview = self.prepare(ROWS + [["", "가상 날짜 누락", "500", "500"]])
        self.assertEqual(self.save(preview).status_code, 400)
        self.assertEqual(self.client.get("/api/data").json["transactions"], [])
        self.assertEqual(self.save(self.prepare(year="")).status_code, 400)

    @patch("gmail_inbox.open_statement", return_value=[ROWS])
    @patch("gmail_inbox.common_password", return_value="fixture-only")
    @patch(
        "gmail_inbox.attachment",
        return_value={"filename": "fixture.html", "data": DATA, "card": "우리"},
    )
    def test_gmail_bulk_check_counts_only_transactions(self, attachment, password, reader):
        result = self.client.post("/api/mail/fixture/check", json={"card": "우리"}, headers=HEADERS)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["count"], 2)
        self.assertEqual(self.client.get("/api/data").json["transactions"], [])
