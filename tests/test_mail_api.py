"""검증 범위: 이메일 명세서 검토 API.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import app, connect
from auth import set_password

HEADERS = {"X-Requested-With": "CardDashboard"}


class MailApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.tmp.name) / "db.sqlite3")
        )
        self.client = app.test_client()
        set_password(connect, "fixture-login-password")

    def tearDown(self):
        self.tmp.cleanup()

    def test_mail_routes_require_login_and_request_protection(self):
        self.assertEqual(self.client.get("/api/mail").status_code, 401)
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)
        self.assertEqual(self.client.post("/api/mail/fake/attachment").status_code, 403)

    @patch(
        "gmail_inbox.attachment",
        return_value={"filename": "fixture.html", "data": b"<script>fixture</script>"},
    )
    def test_html_is_served_as_attachment_and_not_cached(self, reader):
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)
        response = self.client.post("/api/mail/fake/attachment", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.mimetype, "application/octet-stream")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    @patch(
        "gmail_inbox.open_statement",
        return_value=[[["이용일", "가맹점", "금액"], ["2026-10-01", "가상 상점", "42000"]]],
    )
    @patch("gmail_inbox.common_password", return_value="fixture-secret")
    @patch(
        "gmail_inbox.attachment",
        return_value={"filename": "fixture.html", "data": b"fixture", "card": "삼성"},
    )
    def test_saved_password_prepare_returns_preview_without_saving_transactions(
        self, attachment, password, reader
    ):
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)
        result = self.client.post(
            "/api/mail/fake/prepare", json={"card": "삼성", "year": "2026"}, headers=HEADERS
        )
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("fixture-secret", result.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/data").json["transactions"], [])
        self.assertEqual(reader.call_args.args[1], "fixture-secret")

    def test_mail_processing_tracks_preview_save_duplicate_and_delete(self):
        import io
        import os
        import hashlib
        import gmail_inbox as mail

        secret = Path(self.tmp.name) / "mail-secret"
        secret.write_text("abcdefghijklmnop")
        content = b"date,merchant,amount\n2026-10-01,fixture,1000\n"
        digest = hashlib.sha256(content).hexdigest()
        with patch.dict(
            os.environ,
            {
                "MOA_GMAIL_USER": "fixture@gmail.com",
                "MOA_GMAIL_PASSWORD_FILE": str(secret),
                "MOA_STATEMENT_PASSWORD_FILE": str(Path(self.tmp.name) / "absent"),
            },
        ):
            self.client.post(
                "/api/login", json={"password": "fixture-login-password"}, headers=HEADERS
            )
            db = connect()
            mail.schema(db)
            with db:
                db.execute(
                    "INSERT INTO mail_candidates(id,mailbox,validity,uid,part,filename,card,size,digest) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "fixture",
                        mail.config()[3],
                        "1",
                        1,
                        1,
                        "fixture.csv",
                        "신한",
                        len(content),
                        digest,
                    ),
                )
            db.close()

            def status():
                return self.client.get("/api/mail").json

            self.assertEqual(status()["summary"]["received"], 1)
            preview = self.client.post(
                "/api/preview", data={"file": (io.BytesIO(content), "fixture.csv")}, headers=HEADERS
            ).json
            self.assertEqual(status()["items"][0]["stage"], "ready")
            payload = {
                "token": preview["token"],
                "card": "신한",
                "date": 0,
                "merchant": 1,
                "amount": 2,
            }
            self.assertEqual(
                self.client.post(
                    "/api/import", json={**payload, "date": 2}, headers=HEADERS
                ).status_code,
                400,
            )
            self.assertEqual(status()["summary"]["saved"], 0)
            self.assertEqual(
                self.client.post("/api/import", json=payload, headers=HEADERS).status_code, 200
            )
            self.assertEqual(
                self.client.post("/api/import", json=payload, headers=HEADERS).status_code, 409
            )
            item = status()["items"][0]
            self.assertEqual(
                (item["stage"], item["saved_tables"], item["saved_count"]), ("saved", 1, 1)
            )
            self.assertNotIn("digest", item)
            import_id = self.client.get("/api/data").json["imports"][0]["id"]
            self.client.delete("/api/imports/" + str(import_id), headers=HEADERS)
            self.assertEqual(status()["items"][0]["stage"], "ready")

    @patch("gmail_inbox.mark_prepared")
    @patch(
        "gmail_inbox.open_statement",
        return_value=[[["이용일", "가맹점", "금액"], ["2026-10-01", "fixture", "1000"]]],
    )
    @patch("gmail_inbox.common_password", return_value="fixture-secret")
    @patch(
        "gmail_inbox.attachment",
        return_value={"filename": "fixture.html", "data": b"fixture", "card": "삼성"},
    )
    def test_bulk_check_reads_without_preview_tokens_or_ledger_writes(
        self, attachment, password, reader, prepared
    ):
        from app import secure_tables, previews

        self.assertEqual(
            self.client.post(
                "/api/mail/fake/check", json={"card": "삼성"}, headers=HEADERS
            ).status_code,
            401,
        )
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)
        self.assertEqual(
            self.client.post("/api/mail/fake/check", json={"card": "삼성"}).status_code, 403
        )
        tokens = (set(secure_tables), set(previews))
        result = self.client.post("/api/mail/fake/check", json={"card": "삼성"}, headers=HEADERS)
        self.assertEqual(result.json, {"ok": True, "count": 1, "tables": 1})
        self.assertEqual((set(secure_tables), set(previews)), tokens)
        self.assertNotIn("fixture-secret", result.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/data").json["transactions"], [])
        self.assertEqual(result.headers["Cache-Control"], "no-store")

    @patch("gmail_inbox.mark_prepared")
    @patch("gmail_inbox.mark_failed")
    @patch("gmail_inbox.open_statement", side_effect=ValueError("파일 읽기 실패"))
    @patch("gmail_inbox.common_password", return_value="fixture-secret")
    @patch(
        "gmail_inbox.attachment",
        return_value={"filename": "fixture.html", "data": b"fixture", "card": "삼성"},
    )
    def test_failed_bulk_item_does_not_prevent_next_csv_check(
        self, attachment, password, reader, failed, prepared
    ):
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)
        result = self.client.post("/api/mail/first/check", json={"card": "삼성"}, headers=HEADERS)
        self.assertEqual(result.status_code, 400)
        failed.assert_called_once()
        attachment.return_value = {
            "filename": "fixture.csv",
            "data": b"date,merchant,amount\n2026-10-01,fixture,1000\n",
        }
        result = self.client.post("/api/mail/next/check", json={"card": "KB국민"}, headers=HEADERS)
        self.assertEqual(result.json, {"ok": True, "count": 1, "tables": 1})
        prepared.assert_called_once()
        self.assertEqual(password.call_count, 1)
        self.assertEqual(self.client.get("/api/data").json["transactions"], [])

    @patch("gmail_inbox.request_refresh", return_value=7)
    def test_refresh_endpoint_is_authenticated_protected_and_returns_a_ticket(self, queue):
        self.assertEqual(self.client.post("/api/mail/refresh", headers=HEADERS).status_code, 401)
        queue.assert_not_called()
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)
        self.assertEqual(self.client.post("/api/mail/refresh").status_code, 403)
        queue.assert_not_called()
        result = self.client.post("/api/mail/refresh", headers=HEADERS)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.json, {"ticket": 7})
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        queue.assert_called_once()
