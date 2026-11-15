"""가상 카드 응답으로 개인 연결·고정 시각·중복·취소·권한과 원자적 저장을 확인합니다.

실제 CODEF 요청이나 키·명세서는 사용하지 않습니다. 기존 명세서와 겹치는 출처는 자동
합산하지 않고, 네트워크 중 계정 해제·연결 교체 시 지출을 저장하지 않는지 확인합니다.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

from app import app, connect
from auth import set_password
import codef_sync as sync

SETTINGS = dict(
    environment="demo",
    client_id="fixture-id",
    client_secret="fixture-secret",
    public_key="fixture-public",
)
HEADERS = {"X-Requested-With": "CardDashboard"}


def approval(**changes):
    value = dict(
        resUsedDate=datetime.now(sync.KST).strftime("%Y%m%d"),
        resUsedTime="120000",
        resUsedAmount="1200",
        resCancelYN="0",
        resAccountCurrency="KRW",
        resMemberStoreName="가상 상점",
        resCardNo="****0000",
        resApprovalNo="fixture-approval",
    )
    value.update(changes)
    return value


def registration_success():
    return {
        "result": {"code": "CF-00000"},
        "data": {
            "connectedId": "fixture-new-connected",
            "errorList": [],
            "successList": [
                dict(organization="0303", businessType="CD", clientType="P", code="CF-00000")
            ],
        },
    }


class FakeClient:
    def __init__(self, rows):
        self.credentials = Mock(return_value=(SETTINGS, "fixture-token"))
        self.send = Mock(return_value={"result": {"code": "CF-00000"}, "data": rows})


class CodefSyncTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        app.config.update(TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(root / "db.sqlite3"))
        self.key = root / "key"
        self.key.write_bytes(Fernet.generate_key())
        self.environment = patch.dict(
            os.environ,
            {"MOA_CODEF_STORAGE_KEY_FILE": str(self.key), "MOA_CODEF_DAILY_REQUEST_LIMIT": "60"},
        )
        self.environment.start()
        self.configuration = patch.object(sync, "configuration", return_value=SETTINGS)
        self.configuration.start()
        set_password(connect, "fixture-owner-password")
        db = connect()
        with db:
            db.execute(
                "INSERT INTO users(id,username,password_hash,role) VALUES(2,'partner',?,'manager')",
                (generate_password_hash("fixture-partner-password"),),
            )
        db.close()
        sync.save_link(connect, 1, "samsung", "fixture-connected-id", SETTINGS, verifying=False)

    def tearDown(self):
        self.configuration.stop()
        self.environment.stop()
        app.config.update(self.previous)
        self.directory.cleanup()

    def link(self):
        db = connect()
        try:
            return dict(
                db.execute(
                    "SELECT * FROM codef_links WHERE owner_id=1 AND card='samsung'"
                ).fetchone()
            )
        finally:
            db.close()

    def fetch(self, sql, parameters=()):
        db = connect()
        try:
            return db.execute(sql, parameters).fetchall()
        finally:
            db.close()

    def collect(self, rows, slot="fixture-slot"):
        client = FakeClient(rows)
        sync.collect(connect, client, self.link(), slot)
        return client

    def run_operator(self, responses, *, choice="", replace=False, challenge_error=None):
        """실제 네트워크·입력 없이 CLI 전체 흐름과 비밀 값 미출력을 확인합니다."""
        spec = importlib.util.spec_from_file_location(
            "connect_codef_retry",
            Path(__file__).resolve().parents[1] / "scripts/connect-codef-card.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        client = FakeClient([])
        client.send.side_effect = responses
        operator = {
            "read_hidden": Mock(return_value=choice),
            "read_card_payload": Mock(
                return_value=dict(
                    organization="0303",
                    loginType="1",
                    id="fixture-private-bank-id",
                    password="fixture-encrypted-password",
                )
            ),
            "answer_challenge": Mock(return_value="123456", side_effect=challenge_error),
        }
        arguments = ["connect-codef-card.py", "--card", "samsung", "--user", "owner"] + (
            ["--replace"] if replace else []
        )
        with (
            patch.object(cli.sys, "argv", arguments),
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.object(cli.runpy, "run_path", return_value=operator),
            patch.object(cli, "Client", return_value=client),
            patch("builtins.print") as output,
        ):
            result = cli.main()
        printed = " ".join(str(call.args) for call in output.call_args_list)
        self.assertNotIn("fixture-private-bank-id", printed)
        self.assertNotIn("fixture-encrypted-password", printed)
        return result, client, operator, printed

    def test_approval_time_and_last_four_are_saved_and_backfilled(self):
        """승인 시각(HH:MM)과 카드 끝 4자리를 저장하고, 시각 없이 저장된 거래는 재조회 때 채웁니다."""
        self.collect([approval(resUsedTime="143205", resCardNo="5310-12**-****-4321")])
        db = connect()
        row = db.execute("SELECT id,used_time,card_last_four FROM transactions").fetchone()
        self.assertEqual((row["used_time"], row["card_last_four"]), ("14:32", "4321"))
        with db:
            db.execute("UPDATE transactions SET used_time='' WHERE id=?", (row["id"],))
        db.close()
        self.collect(
            [approval(resUsedTime="143205", resCardNo="5310-12**-****-4321")], slot="again"
        )
        db = connect()
        self.assertEqual(
            db.execute("SELECT used_time FROM transactions WHERE id=?", (row["id"],)).fetchone()[0],
            "14:32",
        )
        db.close()

    def test_unreadable_last_four_logs_only_the_mask_shape(self):
        with patch("builtins.print") as printed:
            self.collect([approval(resCardNo="4518-24**-****-123*")])
        message = " ".join(str(arg) for call in printed.call_args_list for arg in call.args)
        self.assertIn("999999******999*", message)
        self.assertNotIn("4518", message)

    def test_data_conflict_is_not_login_failure_and_does_not_request_new_credentials(self):
        result, client, operator, output = self.run_operator(
            [
                {
                    "result": {"code": "CF-00000"},
                    "data": [approval(), approval(resMemberStoreName="다른 가상 상점")],
                }
            ],
            choice="2",
        )
        self.assertEqual(result, 2)
        self.assertEqual(self.link()["state"], "needs_review")
        self.assertIn("가맹점", self.link()["message"])
        self.assertEqual(self.link()["error_code"], "")
        self.assertEqual(client.send.call_count, 1)
        operator["read_card_payload"].assert_not_called()
        operator["read_hidden"].assert_not_called()
        self.assertIn("로그인 정보 오류가 아니라", output)
        self.assertNotIn("다른 가상 상점", output)
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 0)
        self.assertEqual(
            Fernet(self.key.read_bytes()).decrypt(self.link()["cipher"].encode()),
            b"fixture-connected-id",
        )

    def test_missing_card_parameter_does_not_request_another_login(self):
        before = Fernet(self.key.read_bytes()).decrypt(self.link()["cipher"].encode())
        result, client, operator, output = self.run_operator(
            [{"result": {"code": "CF-12401", "extraMessage": "cardNo"}}], choice="2"
        )
        self.assertEqual(result, 2)
        self.assertEqual(client.send.call_count, 1)
        operator["read_hidden"].assert_not_called()
        operator["read_card_payload"].assert_not_called()
        self.assertEqual(
            Fernet(self.key.read_bytes()).decrypt(self.link()["cipher"].encode()), before
        )
        self.assertIn("cardNo", output)

    def test_auth_error_can_be_replaced_once_after_operator_chooses_new_login(self):
        result, client, operator, _ = self.run_operator(
            [
                {"result": {"code": "CF-12801"}},
                registration_success(),
                {"result": {"code": "CF-00000"}, "data": [approval()]},
            ],
            choice="2",
        )
        self.assertEqual(result, 0)
        operator["read_hidden"].assert_called_once()
        operator["read_card_payload"].assert_called_once()
        self.assertEqual(client.send.call_count, 3)
        self.assertEqual(self.link()["state"], "ready")
        self.assertEqual(self.link()["error_code"], "")
        self.assertEqual(
            Fernet(self.key.read_bytes()).decrypt(self.link()["cipher"].encode()),
            b"fixture-new-connected",
        )

    def test_failed_replacement_registration_is_bounded_and_preserves_existing_connection(self):
        failure = {
            "result": {"code": "CF-00000"},
            "data": {
                "successList": [],
                "errorList": [{"code": "CF-19999", "message": "fixture-private-secret"}],
            },
        }
        before = self.link()["cipher"]
        result, client, operator, output = self.run_operator(
            [failure, failure], replace=True, choice="2"
        )
        self.assertEqual(result, 1)
        self.assertEqual(client.send.call_count, 2)
        self.assertEqual(operator["read_card_payload"].call_count, 2)
        operator["read_hidden"].assert_called_once()
        self.assertEqual(self.link()["cipher"], before)
        self.assertNotIn("fixture-private-secret", output)

    def test_new_registration_that_fails_query_auth_restores_previous_connection(self):
        before = self.link()["cipher"]
        result, client, operator, output = self.run_operator(
            [registration_success(), {"result": {"code": "CF-12401", "extraMessage": "password"}}],
            replace=True,
        )
        self.assertEqual(result, 2)
        self.assertEqual(client.send.call_count, 2)
        self.assertEqual(self.link()["cipher"], before)
        self.assertEqual(self.link()["state"], "ready")
        self.assertIn("기존 연결을 복원", output)
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 0)

    def test_generic_registration_failure_does_not_request_another_password(self):
        before = self.link()
        result, client, operator, output = self.run_operator(
            [{"result": {"code": "CF-04000", "message": "fixture-private-login-value"}}],
            replace=True,
            choice="2",
        )
        self.assertEqual(result, 1)
        self.assertEqual(client.send.call_count, 1)
        operator["read_hidden"].assert_not_called()
        self.assertEqual(self.link(), before)
        self.assertIn("비밀번호 오류로 단정하지", output)
        self.assertNotIn("fixture-private-login-value", output)

    def test_generic_registration_failure_from_existing_id_preserves_id_without_retry(self):
        secret = Fernet(self.key.read_bytes())
        before = secret.decrypt(self.link()["cipher"].encode())
        result, client, operator, output = self.run_operator(
            [{"result": {"code": "CF-04000"}}], choice="2"
        )
        self.assertEqual(result, 2)
        self.assertEqual(client.send.call_count, 1)
        self.assertEqual(secret.decrypt(self.link()["cipher"].encode()), before)
        self.assertEqual(self.link()["state"], "error")
        operator["read_hidden"].assert_not_called()
        operator["read_card_payload"].assert_not_called()
        self.assertIn("비밀번호 오류로 단정하지", output)

    def test_failed_replacement_rollback_does_not_undo_concurrent_pause(self):
        spec = importlib.util.spec_from_file_location(
            "connect_codef_restore",
            Path(__file__).resolve().parents[1] / "scripts/connect-codef-card.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        previous = self.link()
        sync.save_link(connect, 1, "samsung", "fixture-candidate", SETTINGS)
        candidate = self.link()
        db = connect()
        with db:
            db.execute("UPDATE codef_links SET enabled=0,version=version+1 WHERE owner_id=1")
        db.close()
        self.assertFalse(cli.restore_failed_replacement(connect, previous, candidate))
        self.assertFalse(self.link()["enabled"])
        self.assertEqual(self.link()["cipher"], candidate["cipher"])

    def test_sms_failure_does_not_turn_into_new_password_prompt(self):
        challenge = {
            "result": {"code": "CF-03002"},
            "data": dict(
                continue2Way=True,
                method="smsAuthNo",
                jobIndex=0,
                threadIndex=0,
                jti="fixture",
                twoWayTimestamp=1,
            ),
        }
        result, client, operator, _ = self.run_operator(
            [challenge, {"result": {"code": "CF-19999"}}], replace=True, choice="2"
        )
        self.assertEqual(result, 3)
        self.assertEqual(client.send.call_count, 2)
        operator["read_hidden"].assert_not_called()

    def test_cancel_during_new_connection_sms_restores_previous_connection(self):
        before = self.link()["cipher"]
        challenge = {
            "result": {"code": "CF-03002"},
            "data": dict(
                continue2Way=True,
                method="smsAuthNo",
                jobIndex=0,
                threadIndex=0,
                jti="fixture",
                twoWayTimestamp=1,
            ),
        }
        result, client, operator, _ = self.run_operator(
            [registration_success(), challenge], replace=True, challenge_error=KeyboardInterrupt
        )
        self.assertEqual(result, 130)
        self.assertEqual(self.link()["cipher"], before)
        self.assertEqual(self.link()["state"], "ready")
        self.assertEqual(client.send.call_count, 2)

    def test_locked_pin_and_network_errors_do_not_prompt_for_another_login(self):
        for response in ({"result": {"code": "CF-12834"}}, None):
            with self.subTest(response=response):
                if response:
                    responses = [response]
                else:
                    from codef_card import CardCheckError

                    responses = [CardCheckError("가상 통신 오류")]
                result, client, operator, _ = self.run_operator(responses, choice="2")
                self.assertEqual(result, 2)
                self.assertEqual(client.send.call_count, 1)
                operator["read_hidden"].assert_not_called()
                operator["read_card_payload"].assert_not_called()

    def test_lotte_registration_waits_for_sms_instead_of_requesting_new_password(self):
        spec = importlib.util.spec_from_file_location(
            "connect_codef_lotte_sms",
            Path(__file__).resolve().parents[1] / "scripts/connect-codef-card.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        client = FakeClient([])
        account = registration_success()
        account["data"]["successList"][0]["organization"] = "0311"
        challenge = {
            "result": {"code": "CF-04034"},
            "data": dict(
                continue2Way=True,
                jobIndex=0,
                threadIndex=0,
                jti="fixture-private",
                twoWayTimestamp=1,
            ),
        }
        client.send.side_effect = [
            challenge,
            account,
            {"result": {"code": "CF-00000"}, "data": [approval()]},
        ]
        operator = {
            "read_card_payload": Mock(
                return_value=dict(
                    organization="0311",
                    loginType="1",
                    id="fixture-private",
                    password="fixture-encrypted",
                )
            ),
            "answer_challenge": Mock(return_value="123456"),
            "read_hidden": Mock(return_value="2"),
        }
        with (
            patch.object(
                cli.sys, "argv", ["connect-codef-card.py", "--card", "lotte", "--user", "owner"]
            ),
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.object(cli.runpy, "run_path", return_value=operator),
            patch.object(cli, "Client", return_value=client),
            patch("builtins.print"),
        ):
            self.assertEqual(cli.main(), 0)
        operator["answer_challenge"].assert_called_once_with("smsAuthNo")
        operator["read_hidden"].assert_not_called()
        self.assertEqual(client.send.call_count, 3)
        self.assertEqual(client.send.call_args_list[1].args[1]["smsAuthNo"], "123456")

    def test_registration_cf04034_without_session_does_not_request_new_credentials(self):
        before = self.link()["cipher"]
        result, client, operator, _ = self.run_operator(
            [{"result": {"code": "CF-04034"}, "data": {}}], replace=True, choice="2"
        )
        self.assertEqual(result, 3)
        self.assertEqual(client.send.call_count, 1)
        operator["read_hidden"].assert_not_called()
        self.assertEqual(self.link()["cipher"], before)

    def test_legacy_conflict_state_is_migrated_without_changing_ledger_or_connection(self):
        before = self.link()["cipher"]
        db = connect()
        with db:
            db.execute("DELETE FROM codef_migrations")
            db.execute(
                "UPDATE codef_links SET state='needs_auth',message=?",
                ("같은 승인 식별자의 응답이 서로 다릅니다. 저장하지 않았습니다.",),
            )
            db.execute("ALTER TABLE codef_links DROP COLUMN error_code")
            # 버전 관리 도입 전 DB는 user_version이 0입니다. 다음 연결에서 이전 단계가 다시 실행됩니다.
            db.execute("PRAGMA user_version = 0")
        db.close()
        self.assertEqual(self.link()["state"], "needs_review")
        self.assertEqual(self.link()["cipher"], before)
        self.assertEqual(self.link()["error_code"], "")
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 0)

    def test_encrypted_connection_and_no_pan_or_approval_number_in_database(self):
        link = self.link()
        self.assertNotIn("fixture-connected-id", link["cipher"])
        self.assertEqual(
            Fernet(self.key.read_bytes()).decrypt(link["cipher"].encode()), b"fixture-connected-id"
        )
        self.collect([approval()])
        db = connect()
        try:
            dump = "\n".join(db.iterdump())
        finally:
            db.close()
        for private in ("fixture-connected-id", "fixture-token", "fixture-approval", "****0000"):
            self.assertNotIn(private, dump)

    def test_fixed_korean_slots_and_utc_conversion(self):
        """변경한 시각 경계와 제거한 정오·17시, UTC의 한국 자정을 확인합니다."""
        for hour, minute, expected in (
            (0, 0, "00"),
            (7, 59, "00"),
            (8, 0, "08"),
            (12, 0, "08"),
            (13, 59, "08"),
            (14, 0, "14"),
            (17, 0, "14"),
            (19, 59, "14"),
            (20, 0, "20"),
            (23, 59, "20"),
        ):
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(
                    sync.latest_slot(datetime(2026, 11, 2, hour, minute, tzinfo=sync.KST)),
                    f"2026-11-02T{expected}:00",
                )
        self.assertEqual(
            sync.latest_slot(datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc)), "2026-11-02T08:00"
        )
        self.assertEqual(
            sync.latest_slot(datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)), "2026-11-02T14:00"
        )
        self.assertEqual(
            sync.latest_slot(datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)), "2026-11-03T00:00"
        )

    def test_restarts_and_repeated_lookup_do_not_duplicate_ledger(self):
        client = self.collect([approval()])
        sync.collect(connect, client, self.link(), "fixture-slot")
        self.assertEqual(client.send.call_count, 1)
        self.collect([approval()], "fixture-next-slot")
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 1)
        self.assertEqual(self.fetch("SELECT amount FROM transactions")[0][0], 1200)
        self.assertEqual(
            self.fetch("SELECT revision FROM personal_revisions WHERE user_id=1")[0][0], 2
        )

    def test_same_amount_merchant_day_with_different_api_identity_are_distinct(self):
        self.collect([approval(), approval(resApprovalNo="fixture-second", resUsedTime="120100")])
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 2)

    def test_exact_partial_and_full_cancel_update_same_transaction(self):
        self.collect([approval()])
        self.collect([approval(resCancelYN="2", resCancelAmount="200")], "partial")
        self.assertEqual(self.fetch("SELECT amount FROM transactions")[0][0], 1000)
        self.collect([approval(resCancelYN="1")], "cancel")
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 1)
        self.assertEqual(self.fetch("SELECT amount FROM transactions")[0][0], 0)

    def test_unmatched_cancel_missing_partial_amount_foreign_and_negative_need_review(self):
        for index, row in enumerate(
            (
                approval(resCancelYN="1"),
                approval(resCancelYN="2"),
                approval(resAccountCurrency="USD"),
                approval(resUsedAmount="-1200"),
            )
        ):
            row["resApprovalNo"] += str(index)
            self.collect([row], "review" + str(index))
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 0)
        self.assertEqual(len(self.fetch("SELECT * FROM codef_events WHERE state='review'")), 4)

    def test_statement_duplicate_is_not_added_to_spending(self):
        db = connect()
        with db:
            source = db.execute(
                "INSERT INTO imports(card,filename,digest,owner_id) VALUES('삼성','가상 명세서','fixture-digest',1)"
            ).lastrowid
            db.execute(
                "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(?,?,?,?)",
                (source, datetime.now(sync.KST).strftime("%Y-%m-%d"), "가상 상점", 1200),
            )
        db.close()
        self.collect([approval()])
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 1)
        self.assertEqual(self.fetch("SELECT reason FROM codef_events")[0][0], "명세서와 중복 후보")

    def test_other_users_statement_does_not_suppress_own_spending(self):
        db = connect()
        with db:
            source = db.execute(
                "INSERT INTO imports(card,filename,digest,owner_id) VALUES('삼성','가상 명세서','fixture-digest',2)"
            ).lastrowid
            db.execute(
                "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(?,?,?,?)",
                (source, datetime.now(sync.KST).strftime("%Y-%m-%d"), "가상 상점", 1200),
            )
        db.close()
        self.collect([approval()])
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 2)
        self.assertEqual(self.fetch("SELECT state FROM codef_events")[0][0], "saved")

    def test_bad_or_conflicting_batch_is_not_partly_saved(self):
        self.collect([approval(), approval(resApprovalNo="", resUsedTime="120100")])
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 0)
        self.assertEqual(self.link()["state"], "needs_review")
        sync.save_link(connect, 1, "samsung", "fixture-connected-id", SETTINGS)
        self.collect([approval(), approval(resMemberStoreName="다른 가상 상점")], "conflict")
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 0)
        for index, invalid in enumerate(
            (approval(resApprovalNo=" "), approval(resUsedAmount="1" * 1000))
        ):
            self.collect([approval(), invalid], "invalid-format-" + str(index))
            self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 0)

    def test_revocation_or_connection_replacement_during_network_discards_result(self):
        for action in (
            "UPDATE users SET active=0 WHERE id=1",
            "UPDATE codef_links SET version=version+1 WHERE owner_id=1",
        ):
            db = connect()
            with db:
                db.execute("UPDATE users SET active=1 WHERE id=1")
            db.close()
            client = FakeClient([])

            def response(token, payload):
                db = connect()
                with db:
                    db.execute(action)
                db.close()
                return {"result": {"code": "CF-00000"}, "data": [approval()]}

            client.send.side_effect = response
            sync.collect(connect, client, self.link(), action)
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 0)

    def test_worker_stops_for_auth_and_does_not_save_raw_provider_text(self):
        client = FakeClient([])
        client.send.return_value = {
            "result": {"code": "CF-03002"},
            "data": dict(
                continue2Way=True,
                method="smsAuthNo",
                jobIndex=0,
                threadIndex=0,
                jti="fixture-private",
                twoWayTimestamp=1,
            ),
        }
        sync.collect(connect, client, self.link(), "auth")
        self.assertEqual(client.send.call_count, 1)
        self.assertEqual(self.link()["state"], "needs_auth")
        sync.tick(connect, client)
        self.assertEqual(client.send.call_count, 1)
        self.assertNotIn("fixture-private", self.link()["message"])

    def test_budget_is_atomic_and_counts_failed_attempts_before_network(self):
        with patch.dict(os.environ, {"MOA_CODEF_DAILY_REQUEST_LIMIT": "2"}):

            def reserve(index):
                try:
                    sync.reserve_request(connect)
                    return True
                except sync.BudgetError:
                    return False

            with ThreadPoolExecutor(4) as pool:
                results = list(pool.map(reserve, range(4)))
            self.assertEqual(sum(results), 2)
        self.assertEqual(self.fetch("SELECT requests FROM codef_budget")[0][0], 2)

    def test_tick_catches_up_once_and_manual_request_covers_current_slot(self):
        db = connect()
        with db:
            db.execute("UPDATE codef_links SET requested=?", (int(datetime.now().timestamp()),))
        db.close()
        client = FakeClient([approval()])
        now = datetime(2026, 11, 2, 8, 5, tzinfo=sync.KST)
        sync.tick(connect, client, now)
        sync.tick(connect, client, now)
        self.assertEqual(client.send.call_count, 1)
        for hour, expected_calls in ((12, 1), (14, 2), (17, 2), (20, 3)):
            sync.tick(connect, client, datetime(2026, 11, 2, hour, 5, tzinfo=sync.KST))
            self.assertEqual(client.send.call_count, expected_calls)
        sync.tick(connect, client, datetime(2026, 11, 3, 0, 5, tzinfo=sync.KST))
        self.assertEqual(client.send.call_count, 4)

    def test_deleted_auto_import_is_not_silently_restored(self):
        self.collect([approval()])
        db = connect()
        with db:
            db.execute("DELETE FROM imports WHERE digest LIKE 'codef:%'")
        db.close()
        self.collect([approval()], "after-delete")
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 0)
        self.assertEqual(self.fetch("SELECT state FROM codef_events")[0][0], "review")

    def test_private_api_hides_other_user_connection_and_cipher_and_requires_revision(self):
        self.collect([approval(resCancelYN="2")])
        owner = app.test_client()
        owner.post("/api/login", json={"password": "fixture-owner-password"}, headers=HEADERS)
        partner = app.test_client()
        partner.post(
            "/api/login",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers=HEADERS,
        )
        result = owner.get("/api/codef-sync")
        self.assertEqual(len(result.json["links"]), 1)
        self.assertEqual(result.json["schedule"], "한국시간 00:00·08:00·14:00·20:00")
        self.assertNotIn("cipher", result.get_data(as_text=True))
        self.assertNotIn("fixture-connected-id", result.get_data(as_text=True))
        self.assertEqual(partner.get("/api/codef-sync").json["links"], [])
        event = result.json["review"][0]["id"]
        revision = partner.get("/api/codef-sync").headers["X-Moa-Revision"]
        self.assertEqual(
            partner.post(
                f"/api/codef-sync/review/{event}",
                json={"action": "ignore"},
                headers={**HEADERS, "If-Match": revision},
            ).status_code,
            404,
        )
        self.assertEqual(owner.post("/api/codef-sync/refresh", headers=HEADERS).status_code, 428)
        revision = owner.get("/api/codef-sync").headers["X-Moa-Revision"]
        self.assertEqual(
            owner.post(
                "/api/codef-sync/refresh", headers={**HEADERS, "If-Match": revision}
            ).status_code,
            200,
        )
        self.assertEqual(
            owner.post(
                "/api/codef-sync/pause",
                json={"card": "samsung"},
                headers={**HEADERS, "If-Match": revision},
            ).status_code,
            409,
        )

    def test_init_key_does_not_replace_existing_key(self):
        spec = importlib.util.spec_from_file_location(
            "connect_codef_fixture",
            Path(__file__).resolve().parents[1] / "scripts/connect-codef-card.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with (
            patch.dict(os.environ, {"MOA_CODEF_SETUP_DIR": self.directory.name}),
            patch.object(cli.os, "chown"),
            patch("builtins.print"),
        ):
            cli.init_key()
            path = Path(self.directory.name) / "codef-storage.key"
            original = path.read_bytes()
            cli.init_key()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_private_ui_requests_and_revision_mapping(self):
        subprocess.run(
            ["node", "tests/codef-sync-ui.cjs"], check=True, capture_output=True, timeout=20
        )

    def test_registration_verifies_institution_success_and_uses_connected_id_only(self):
        spec = importlib.util.spec_from_file_location(
            "connect_codef_register",
            Path(__file__).resolve().parents[1] / "scripts/connect-codef-card.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        client = FakeClient([])
        client.send.side_effect = [
            {
                "result": {"code": "CF-00000"},
                "data": {
                    "connectedId": "fixture-new-connected",
                    "errorList": [],
                    "successList": [
                        dict(
                            organization="0303", businessType="CD", clientType="P", code="CF-00000"
                        )
                    ],
                },
            },
            {"result": {"code": "CF-00000"}, "data": [approval()]},
        ]
        operator = {
            "read_card_payload": Mock(
                return_value=dict(
                    organization="0303",
                    loginType="1",
                    id="fixture-bank-id",
                    password="fixture-encrypted-password",
                )
            ),
            "answer_challenge": Mock(),
        }
        with (
            patch.object(
                cli.sys,
                "argv",
                ["connect-codef-card.py", "--card", "samsung", "--user", "owner", "--replace"],
            ),
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.object(cli.runpy, "run_path", return_value=operator),
            patch.object(cli, "Client", return_value=client),
            patch("builtins.print") as output,
        ):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(client.send.call_args_list[0].args[2], "register")
        queried = client.send.call_args_list[1].args[1]
        self.assertEqual(queried["connectedId"], "fixture-new-connected")
        self.assertNotIn("password", queried)
        self.assertNotIn("id", queried)
        text = " ".join(str(call.args) for call in output.call_args_list)
        self.assertNotIn("fixture-bank-id", text)
        self.assertNotIn("fixture-new-connected", text)
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 1)

    def test_client_caches_token_in_memory_and_reserves_each_provider_call(self):
        client = sync.Client(connect)
        with (
            patch.object(sync, "configuration", return_value=SETTINGS),
            patch.object(sync, "issue_token", return_value="fixture-token") as token,
            patch.object(
                sync, "post_codef", return_value={"result": {"code": "CF-00000"}, "data": []}
            ),
        ):
            client.credentials()
            client.send("fixture-token", {})
            client.credentials()
            client.send("fixture-token", {})
        token.assert_called_once()
        self.assertEqual(self.fetch("SELECT requests FROM codef_budget")[0][0], 3)

    def test_operator_cancel_does_not_register_or_replace_connection(self):
        spec = importlib.util.spec_from_file_location(
            "connect_codef_cancel",
            Path(__file__).resolve().parents[1] / "scripts/connect-codef-card.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        old = self.link()["cipher"]
        client = FakeClient([])
        operator = {
            "read_card_payload": Mock(side_effect=KeyboardInterrupt),
            "answer_challenge": Mock(),
        }
        with (
            patch.object(
                cli.sys,
                "argv",
                ["connect-codef-card.py", "--card", "samsung", "--user", "owner", "--replace"],
            ),
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.object(cli.runpy, "run_path", return_value=operator),
            patch.object(cli, "Client", return_value=client),
            patch("builtins.print"),
        ):
            self.assertEqual(cli.main(), 130)
        client.send.assert_not_called()
        self.assertEqual(self.link()["cipher"], old)


if __name__ == "__main__":
    unittest.main()
