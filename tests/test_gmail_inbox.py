"""검증 범위: 가상 Gmail 수집·발신 인증·첨부 처리.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import io
import os
import sqlite3
import tempfile
import unittest
from email.message import EmailMessage
from email.parser import BytesParser
import email.policy
from pathlib import Path
from unittest.mock import patch

import gmail_inbox as mail


def message(name="samsung.html"):
    item = EmailMessage()
    item["From"] = "billing@samsungcard.com"
    item["Subject"] = "삼성카드 명세서"
    item.set_content("not retained")
    item.add_attachment(
        b"<html>synthetic</html>", maintype="application", subtype="octet-stream", filename=name
    )
    item.add_attachment(
        b"not supported", maintype="application", subtype="octet-stream", filename="run.exe"
    )
    return item.as_bytes()


class FakeImap:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.validity = b"17"

    def login(self, *args):
        self.calls.append(("login",))
        return "OK", []

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", []

    def response(self, name):
        return name, [self.validity]

    def logout(self):
        self.calls.append(("logout",))

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            return "OK", [b"1 2"]
        if args[-1] == "(RFC822.SIZE)":
            return "OK", [b"1 (RFC822.SIZE 1000)"]
        return "OK", [(b"1", message())]


class GmailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.secret = self.root / "secret"
        self.secret.write_text("abcdefghijklmnop")
        self.env = patch.dict(
            os.environ,
            {
                "MOA_GMAIL_USER": "fixture@gmail.com",
                "MOA_GMAIL_FOLDER": "MOA",
                "MOA_GMAIL_PASSWORD_FILE": str(self.secret),
                "MOA_STATEMENT_PASSWORD_FILE": str(self.root / "statement"),
            },
        )
        self.env.start()
        self.client = FakeImap()
        self.network = patch("gmail_inbox.imaplib.IMAP4_SSL", return_value=self.client)
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.env.stop()
        self.tmp.cleanup()

    def connect(self):
        db = sqlite3.connect(self.root / "mail.sqlite3")
        db.row_factory = sqlite3.Row
        return db

    def test_readonly_sync_restart_and_lazy_fetch(self):
        self.assertEqual(mail.sync(self.connect), 2)
        self.assertEqual(mail.sync(self.connect), 0)
        state = mail.inbox(self.connect)
        self.assertEqual(len(state["items"]), 2)
        self.assertEqual(state["items"][0]["card"], "삼성")
        result = mail.attachment(self.connect, state["items"][0]["id"])
        self.assertEqual(result["data"], b"<html>synthetic</html>")
        self.assertIn(("select", '"MOA"', True), self.client.calls)
        self.assertTrue(
            all(call[0] not in ["store", "expunge", "append", "copy"] for call in self.client.calls)
        )
        fetches = [call for call in self.client.calls if call[0] == "fetch"]
        self.assertTrue(all(call[-1] in ["(RFC822.SIZE)", "(BODY.PEEK[])"] for call in fetches))
        raw = (self.root / "mail.sqlite3").read_bytes()
        self.assertNotIn(b"<html>synthetic</html>", raw)
        self.assertNotIn(b"abcdefghijklmnop", raw)

    def test_uid_validity_change_rejects_old_candidate(self):
        mail.sync(self.connect)
        identifier = mail.inbox(self.connect)["items"][0]["id"]
        self.client.validity = b"18"
        with self.assertRaisesRegex(mail.MailError, "메일함이 변경"):
            mail.attachment(self.connect, identifier)

    def test_mime_filter_filename_and_untrusted_brand_guess(self):
        result = mail.parts(message("../samsung.html"))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["filename"], "samsung.html")
        self.assertEqual(result[0]["card"], "삼성")

    def test_authentication_error_is_sanitized(self):
        with patch.object(
            self.client, "login", side_effect=mail.imaplib.IMAP4.error("SECRET_AUTH_DETAIL")
        ):
            with self.assertRaises(mail.MailError) as caught:
                mail.sync(self.connect)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("abcdefghijklmnop", str(caught.exception))

    def test_secret_is_required_and_not_in_status(self):
        self.secret.unlink()
        self.assertFalse(mail.inbox(self.connect)["configured"])

    def test_auto_password_is_used_only_for_authenticated_issuer_mail(self):
        secret = self.root / "statement"
        secret.write_text("fixture-password")
        raw = message()
        item = mail.parts(raw)[0]
        with (
            patch.dict(os.environ, {"MOA_STATEMENT_PASSWORD_FILE": str(secret)}),
            patch("gmail_inbox.open_statement") as reader,
        ):
            count, error = mail.automatic_check(item, raw)
            self.assertIsNone(count)
            self.assertIsNotNone(error)
            reader.assert_not_called()
            authenticated = BytesParser(policy=email.policy.default).parsebytes(raw)
            authenticated["Authentication-Results"] = (
                "mx.google.com; dmarc=pass header.from=samsungcard.com;"
            )
            reader.return_value = [
                [["날짜", "가맹점", "금액"], ["2026-10-01", "가상 상점", "42000"]]
            ]
            count, error = mail.automatic_check(item, authenticated.as_bytes())
            self.assertEqual(count, 1)
            self.assertIsNone(error)
            self.assertEqual(reader.call_args.args[1], "fixture-password")

    def test_common_password_is_not_exposed_in_inbox_status(self):
        secret = self.root / "statement"
        secret.write_text("fixture-password")
        with patch.dict(os.environ, {"MOA_STATEMENT_PASSWORD_FILE": str(secret)}):
            result = mail.inbox(self.connect)
        self.assertTrue(result["auto_password"])
        self.assertNotIn("fixture-password", str(result))

    def test_processing_summary_and_reading_preserves_confirmed_state(self):
        mail.sync(self.connect)
        state = mail.inbox(self.connect)
        self.assertEqual(state["summary"]["received"], 2)
        identifier = state["items"][0]["id"]
        mail.mark_prepared(self.connect, identifier, "삼성", [[["date"], ["fixture"]]])
        mail.attachment(self.connect, identifier)
        self.assertEqual(mail.inbox(self.connect)["summary"]["ready"], 1)
        db = self.connect()
        with db:
            db.execute(
                "UPDATE mail_candidates SET auto_error='검토 필요' WHERE id=?", (identifier,)
            )
        db.close()
        self.assertEqual(mail.inbox(self.connect)["summary"]["needs_review"], 1)

    def test_invalid_common_password_does_not_block_collection(self):
        (self.root / "statement").write_text("")
        self.assertEqual(mail.sync(self.connect), 2)
        state = mail.inbox(self.connect)
        self.assertEqual(state["summary"]["needs_review"], 2)
        self.assertIsNotNone(state["password_error"])

    def test_existing_schema_migrates_without_losing_candidates(self):
        mail.sync(self.connect)
        db = self.connect()
        db.execute("ALTER TABLE mail_candidates DROP COLUMN digest")
        db.close()
        state = mail.inbox(self.connect)
        self.assertEqual(state["total"], 2)
        self.assertEqual(state["summary"]["saved"], 0)
        mail.attachment(self.connect, state["items"][0]["id"])
        db = self.connect()
        self.assertIsNotNone(
            db.execute(
                "SELECT digest FROM mail_candidates WHERE id=?", (state["items"][0]["id"],)
            ).fetchone()[0]
        )
        db.close()

    def test_secure_table_saves_are_counted_without_claiming_whole_file(self):
        import hashlib

        mail.sync(self.connect)
        digest = hashlib.sha256(b"<html>synthetic</html>").hexdigest()
        db = self.connect()
        db.executescript(
            "CREATE TABLE imports(id INTEGER PRIMARY KEY,digest TEXT,created TEXT,owner_id INTEGER NOT NULL DEFAULT 1); CREATE TABLE transactions(id INTEGER PRIMARY KEY,import_id INTEGER);"
        )
        with db:
            db.executemany(
                "INSERT INTO imports(id,digest,created) VALUES(?,?,?)",
                [
                    (1, digest + ":table1", "2026-10-30 00:00:00"),
                    (2, digest + ":table2", "2026-10-30 01:00:00"),
                ],
            )
            db.executemany("INSERT INTO transactions VALUES(?,?)", [(1, 1), (2, 1), (3, 2)])
        db.close()
        state = mail.inbox(self.connect)
        self.assertEqual(state["summary"]["saved"], 2)  # identical files in two messages
        self.assertEqual(
            (state["items"][0]["saved_tables"], state["items"][0]["saved_count"]), (2, 3)
        )
        self.assertEqual(state["items"][0]["saved_at"], "2026-10-30 01:00:00")
        mail.mark_failed(self.connect, digest)
        self.assertEqual(mail.inbox(self.connect)["items"][0]["stage"], "saved")
        mail.mark_read(self.connect, digest)
        self.assertIsNone(mail.inbox(self.connect)["items"][0]["auto_error"])

    def test_woori_html_dispatch_and_authenticated_auto_open(self):
        raw = message("WooriCard.html")
        message_object = BytesParser(policy=email.policy.default).parsebytes(raw)
        message_object.replace_header("From", "billing@wooricard.com")
        message_object.replace_header("Subject", "우리카드 명세서")
        message_object["Authentication-Results"] = (
            "mx.google.com; dmarc=pass header.from=wooricard.com;"
        )
        raw = message_object.as_bytes()
        item = mail.parts(raw)[0]
        self.assertEqual(item["card"], "우리")
        (self.root / "statement").write_text("fixture-only")
        with patch(
            "secure_html.unlock_statement",
            return_value=[[["이용일", "가맹점", "금액"], ["2026-10-01", "fixture", "1000"]]],
        ) as reader:
            count, error = mail.automatic_check(item, raw)
            self.assertEqual(count, 1)
            self.assertIsNone(error)
            reader.assert_called_once_with(
                item["data"], "fixture-only", card="우리", allow_online=False
            )

    def test_manual_refresh_is_queued_coalesced_and_completes_without_waiting_schedule(self):
        self.assertIsNone(mail.process_refresh(self.connect))
        self.assertFalse(self.client.calls)
        self.assertEqual(mail.request_refresh(self.connect), 1)
        self.assertEqual(mail.request_refresh(self.connect), 1)
        self.assertEqual(mail.process_refresh(self.connect), {"ok": True, "found": 2})
        state = mail.inbox(self.connect)["refresh"]
        self.assertEqual((state["requested"], state["completed"], state["running"]), (1, 1, 0))
        self.assertTrue(state["worker_online"])
        self.assertIsNone(mail.process_refresh(self.connect))
        self.assertEqual(mail.request_refresh(self.connect), 2)
        self.assertEqual(mail.process_refresh(self.connect), {"ok": True, "found": 0})

    def test_manual_refresh_drains_backlog_instead_of_waiting_five_minutes_per_batch(self):
        original = self.client.uid

        def many(command, *args):
            if command == "search":
                return "OK", [b" ".join(str(n).encode() for n in range(1, 26))]
            return original(command, *args)

        self.client.uid = many
        mail.request_refresh(self.connect)
        self.assertEqual(mail.process_refresh(self.connect), {"ok": True, "found": 25})
        self.assertEqual(mail.inbox(self.connect)["total"], 25)
        self.assertEqual(mail.inbox(self.connect)["refresh"]["remaining"], 0)

    def test_refresh_queued_during_scheduled_work_is_not_lost(self):
        original = mail.sync

        def during(connect):
            mail.request_refresh(connect)
            return original(connect)

        with patch("gmail_inbox.sync", side_effect=during):
            mail.process_refresh(self.connect, automatic=True)
        state = mail.inbox(self.connect)["refresh"]
        self.assertEqual((state["requested"], state["completed"]), (1, 0))
        self.assertEqual(mail.process_refresh(self.connect), {"ok": True, "found": 0})
        self.assertEqual(mail.inbox(self.connect)["refresh"]["completed"], 1)

    def test_failed_refresh_finishes_with_sanitized_error_and_can_retry(self):
        mail.request_refresh(self.connect)
        with patch.object(
            self.client, "login", side_effect=mail.imaplib.IMAP4.error("PRIVATE_SECRET")
        ):
            self.assertFalse(mail.process_refresh(self.connect)["ok"])
        state = mail.inbox(self.connect)["refresh"]
        self.assertEqual(state["completed"], 1)
        self.assertEqual(state["running"], 0)
        self.assertNotIn("PRIVATE_SECRET", state["error"])
        self.assertEqual(mail.request_refresh(self.connect), 2)

    def test_offline_worker_is_reported_using_server_time(self):
        with patch("gmail_inbox.time.time", return_value=100):
            mail.request_refresh(self.connect)
        with patch("gmail_inbox.time.time", return_value=110):
            self.assertFalse(mail.inbox(self.connect)["refresh"]["unresponsive"])
        with patch("gmail_inbox.time.time", return_value=120):
            self.assertTrue(mail.inbox(self.connect)["refresh"]["unresponsive"])
        mail.process_refresh(self.connect)
        self.assertFalse(mail.inbox(self.connect)["refresh"]["unresponsive"])
