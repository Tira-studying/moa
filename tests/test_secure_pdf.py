"""검증 범위: PDF 판독·암호 오류·처리 한도.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import io
import tempfile
import unittest
from pathlib import Path

from app import app, connect, previews, secure_tables
from auth import set_password
from secure_pdf import unlock_pdf

FIXTURES = Path(__file__).parent / "fixtures"
HEADERS = {"X-Requested-With": "CardDashboard"}


class PdfReaderTests(unittest.TestCase):
    def test_plain_pdf_reads_table(self):
        rows = unlock_pdf((FIXTURES / "shinhan-plain.pdf").read_bytes())[0]
        self.assertEqual(rows[0], ["Date", "Merchant", "Amount"])
        self.assertEqual(rows[1], ["2026-10-03", "Synthetic Shop", "42,000"])

    def test_encrypted_pdf_and_refund(self):
        rows = unlock_pdf((FIXTURES / "shinhan-encrypted.pdf").read_bytes(), "fixture-only")[0]
        self.assertEqual(rows[-1][-1], "-12,000")

    def test_password_required_and_wrong_password_are_sanitized(self):
        data = (FIXTURES / "shinhan-encrypted.pdf").read_bytes()
        for password in ["", "fixture-wrong"]:
            with self.assertRaisesRegex(ValueError, "비밀번호") as caught:
                unlock_pdf(data, password)
            self.assertNotIn("fixture-wrong", str(caught.exception))

    def test_invalid_pdf_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "유효한 PDF"):
            unlock_pdf(b"<html>not pdf</html>")


class PdfApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.tmp.name) / "test.sqlite3")
        )
        self.client = app.test_client()
        previews.clear()
        secure_tables.clear()
        set_password(connect, "fixture-login-password")
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self, card="신한", password="fixture-only"):
        return self.client.post(
            "/api/secure-html",
            data={
                "card": card,
                "file": (
                    io.BytesIO((FIXTURES / "shinhan-encrypted.pdf").read_bytes()),
                    "fixture.pdf",
                ),
                "password": password,
            },
            headers=HEADERS,
        )

    def test_pdf_preview_save_card_lock_and_duplicate(self):
        response = self.submit()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/data").json["transactions"], [])
        self.assertNotIn("fixture-only", str(secure_tables))
        token = response.json["token"]
        preview = self.client.post(
            "/api/secure-preview",
            json={"token": token, "table": 0, "header": 1, "end": 3},
            headers=HEADERS,
        ).json
        payload = {"token": preview["token"], "card": "삼성", "date": 0, "merchant": 1, "amount": 2}
        self.assertEqual(
            self.client.post("/api/import", json=payload, headers=HEADERS).status_code, 400
        )
        payload["card"] = "신한"
        self.assertEqual(
            self.client.post("/api/import", json=payload, headers=HEADERS).status_code, 200
        )
        rows = self.client.get("/api/data").json["transactions"]
        self.assertEqual(sum(row["amount"] for row in rows), 30000)
        self.assertTrue(all(row["card"] == "신한" for row in rows))
        self.assertEqual(
            self.client.post("/api/import", json=payload, headers=HEADERS).status_code, 409
        )

    def test_wrong_password_saves_nothing(self):
        self.assertEqual(self.submit(password="fixture-wrong").status_code, 400)
        self.assertEqual(self.client.get("/api/data").json["transactions"], [])
        self.assertEqual(secure_tables, {})

    def test_pdf_cannot_be_read_as_another_card(self):
        response = self.submit(card="삼성")
        self.assertEqual(response.status_code, 400)
        self.assertIn("신한카드는 PDF", response.json["error"])
