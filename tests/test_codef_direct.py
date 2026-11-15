"""가상 응답으로 등록 없는 직접 조회·저장의 소유권·SMS·중복 및 저장 전 재검사를 확인합니다.

실제 CODEF 요청이나 금융 정보를 사용하지 않습니다. 수동 조회가 예약 연결을 만들거나
기존 연결을 수정하지 않고, 인증 정보는 원장·수동 처리현황에 남기지 않는지 검증합니다.
"""

from copy import deepcopy
from datetime import datetime
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

from app import app, connect
from auth import set_password
from codef_card import CardCheckError
import codef_direct as direct
import codef_sync as sync


SETTINGS = dict(
    environment="demo",
    client_id="fixture-id",
    client_secret="fixture-secret",
    public_key="fixture-public",
)
HEADERS = {"X-Requested-With": "CardDashboard"}


def fixture_row(**changes):
    """식별자와 금액은 이 테스트에서만 사용하는 가상 값입니다."""
    row = dict(
        resUsedDate=datetime.now(sync.KST).strftime("%Y%m%d"),
        resUsedTime="120000",
        resUsedAmount="2400",
        resCancelYN="0",
        resAccountCurrency="KRW",
        resMemberStoreName="가상 문구점",
        resCardNo="****0000",
        resApprovalNo="fixture-approval-private",
    )
    row.update(changes)
    return row


def success(rows=None):
    return {"result": {"code": "CF-00000"}, "data": rows if rows is not None else [fixture_row()]}


class FakeClient:
    def __init__(self, response=None):
        self.credentials = Mock(return_value=(SETTINGS, "fixture-token-private"))
        self.send = Mock(return_value=response if response is not None else success())


class CodefDirectTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        app.config.update(TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(root / "db.sqlite3"))
        self.key = root / "fixture-key"
        self.key.write_bytes(Fernet.generate_key())
        self.environment = patch.dict(os.environ, {"MOA_CODEF_STORAGE_KEY_FILE": str(self.key)})
        self.environment.start()
        self.configuration = patch.object(direct, "configuration", return_value=SETTINGS)
        self.settings_reader = self.configuration.start()
        self.sync_configuration = patch.object(sync, "configuration", return_value=SETTINGS)
        self.sync_configuration.start()
        set_password(connect, "fixture-owner-password")
        self.execute(
            "INSERT INTO users(id,username,password_hash,role) VALUES(2,'partner',?,'manager')",
            (generate_password_hash("fixture-partner-password"),),
        )

    def tearDown(self):
        self.sync_configuration.stop()
        self.configuration.stop()
        self.environment.stop()
        app.config.update(self.previous)
        self.directory.cleanup()

    def execute(self, sql, parameters=()):
        """테스트 중 별도 작업자가 바꾼 상태를 독립된 저장 단위로 흉내 냅니다."""
        db = connect()
        try:
            with db:
                db.execute(sql, parameters)
        finally:
            db.close()

    def fetch(self, sql, parameters=()):
        db = connect()
        try:
            return db.execute(sql, parameters).fetchall()
        finally:
            db.close()

    def operator(self, card="lotte"):
        payload = dict(
            organization=sync.CARD_OPTIONS[card][0],
            loginType="1",
            id="fixture-bank-id-private",
            password="fixture-encrypted-password-private",
        )
        return {
            "read_card_payload": Mock(return_value=payload),
            "answer_challenge": Mock(return_value="123456"),
            "read_hidden": Mock(),
        }, payload

    def collect(self, client=None, owner=1, card="lotte"):
        operator, payload = self.operator(card)
        with patch("builtins.print") as output:
            result = direct.collect_direct(connect, client or FakeClient(), owner, card, operator)
        return result, operator, payload, output

    def assert_nothing_saved(self):
        for table in ("transactions", "codef_events", "codef_manual_runs"):
            self.assertEqual(len(self.fetch("SELECT * FROM " + table)), 0)

    def test_direct_success_is_idempotent_and_creates_no_auto_connection_or_secret_record(self):
        client = FakeClient()
        result, operator, payload, output = self.collect(client)
        self.assertEqual(result, 1)
        self.assertEqual(payload, {})
        self.assertEqual(len(self.fetch("SELECT * FROM codef_links")), 0)
        self.assertEqual(len(self.fetch("SELECT * FROM codef_jobs")), 0)
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 1)
        self.assertEqual(self.fetch("SELECT group_count FROM codef_manual_runs")[0][0], 1)
        operator["answer_challenge"].assert_not_called()
        args = operator["read_card_payload"].call_args.args
        self.assertEqual(args[1], "lotte")
        self.assertEqual(
            (datetime.strptime(args[3], "%Y%m%d") - datetime.strptime(args[2], "%Y%m%d")).days, 6
        )
        self.collect(client)
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 1)
        self.assertEqual(len(self.fetch("SELECT * FROM codef_events")), 1)
        db = connect()
        try:
            dump = "\n".join(db.iterdump())
        finally:
            db.close()
        printed = str(output.call_args_list)
        for secret in (
            "fixture-bank-id-private",
            "fixture-encrypted-password-private",
            "fixture-token-private",
            "fixture-approval-private",
        ):
            self.assertNotIn(secret, dump)
            self.assertNotIn(secret, printed)

    def test_lotte_sms_waits_then_sends_exact_metadata_to_same_product(self):
        metadata = dict(jobIndex=0, threadIndex=0, jti="fixture-jti-private", twoWayTimestamp=1)
        responses = [
            {
                "result": {"code": "CF-03002"},
                "data": dict(continue2Way=True, commSession="fixture-session-private", **metadata),
            },
            success(),
        ]
        sent = []
        client = FakeClient()

        def send(token, payload, operation="approval"):
            sent.append((operation, deepcopy(payload)))
            return responses[len(sent) - 1]

        client.send.side_effect = send
        operator, payload = self.operator()

        def answer(method):
            self.assertEqual(method, "smsAuthNo")
            self.assertEqual(len(sent), 1)
            self.assert_nothing_saved()
            return "123456"

        operator["answer_challenge"].side_effect = answer
        with patch("builtins.print"):
            self.assertEqual(direct.collect_direct(connect, client, 1, "lotte", operator), 1)
        self.assertEqual([row[0] for row in sent], ["approval", "approval"])
        self.assertEqual(sent[1][1]["twoWayInfo"], metadata)
        self.assertTrue(sent[1][1]["is2Way"])
        self.assertEqual(sent[1][1]["smsAuthNo"], "123456")
        self.assertEqual(sent[1][1]["commSession"], "fixture-session-private")
        self.assertNotIn("accountList", sent[0][1])
        self.assertEqual(payload, {})
        self.assertEqual(len(self.fetch("SELECT * FROM codef_links")), 0)

    def test_direct_and_connected_collection_share_ledger_identity(self):
        self.collect(card="samsung")
        sync.save_link(connect, 1, "samsung", "fixture-connected-id", SETTINGS, verifying=False)
        link = dict(self.fetch("SELECT * FROM codef_links")[0])
        sync.collect(connect, FakeClient(), link, "fixture-slot")
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 1)
        self.assertEqual(len(self.fetch("SELECT * FROM codef_events")), 1)

    def test_paused_connection_and_other_owner_records_are_preserved(self):
        sync.save_link(connect, 1, "lotte", "fixture-cid-owner", SETTINGS, verifying=False)
        sync.save_link(connect, 2, "lotte", "fixture-cid-partner", SETTINGS, verifying=False)
        self.execute("UPDATE codef_links SET enabled=0,version=version+1 WHERE owner_id=1")
        before = [dict(row) for row in self.fetch("SELECT * FROM codef_links ORDER BY owner_id")]
        self.collect()
        self.assertEqual(
            before, [dict(row) for row in self.fetch("SELECT * FROM codef_links ORDER BY owner_id")]
        )
        self.collect(owner=2)
        self.assertEqual(
            [
                row[0]
                for row in self.fetch("SELECT owner_id FROM codef_manual_runs ORDER BY owner_id")
            ],
            [1, 2],
        )
        self.assertEqual(
            [row[0] for row in self.fetch("SELECT owner_id FROM imports ORDER BY owner_id")], [1, 2]
        )
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 2)

    def test_revoked_account_during_request_discards_all_results(self):
        client = FakeClient()

        def send(*args):
            self.execute("UPDATE users SET active=0 WHERE id=1")
            return success()

        client.send.side_effect = send
        with self.assertRaises(sync.SyncError):
            self.collect(client)
        self.assert_nothing_saved()

    def test_link_created_replaced_or_paused_during_request_discards_all_results(self):
        for change in ("create", "replace", "pause"):
            with self.subTest(change=change):
                self.execute("DELETE FROM codef_links")
                if change != "create":
                    sync.save_link(connect, 1, "lotte", "fixture-cid", SETTINGS, verifying=False)
                client = FakeClient()

                def send(*args):
                    if change == "pause":
                        self.execute(
                            "UPDATE codef_links SET enabled=0,version=version+1 WHERE owner_id=1"
                        )
                    else:
                        sync.save_link(
                            connect, 1, "lotte", "fixture-new-cid", SETTINGS, verifying=False
                        )
                    return success()

                client.send.side_effect = send
                with self.assertRaises(sync.SyncError):
                    self.collect(client)
                self.assert_nothing_saved()

    def test_invalid_second_row_prevents_partial_ledger_or_success_status(self):
        client = FakeClient(
            success([fixture_row(), fixture_row(resApprovalNo="", resUsedAmount="5000")])
        )
        with self.assertRaises(sync.DataReviewError):
            self.collect(client)
        self.assert_nothing_saved()

    def test_save_failure_rolls_back_written_ledger_and_manual_success_together(self):
        def store_then_fail(db, owner, card, items):
            sync.store_events(db, owner, card, items)
            raise sync.SyncError("가상 저장 중단")

        with (
            patch.object(direct, "store_events", side_effect=store_then_fail),
            self.assertRaises(sync.SyncError),
        ):
            self.collect()
        self.assert_nothing_saved()
        self.assertEqual(len(self.fetch("SELECT * FROM imports")), 0)

    def test_incomplete_sms_response_does_not_prompt_or_create_manual_success(self):
        client = FakeClient({"result": {"code": "CF-04034"}, "data": {}})
        operator, payload = self.operator()
        with patch("builtins.print"), self.assertRaises(CardCheckError):
            direct.collect_direct(connect, client, 1, "lotte", operator)
        client.send.assert_called_once()
        operator["answer_challenge"].assert_not_called()
        self.assertEqual(payload, {})
        self.assert_nothing_saved()

    def test_keys_or_configuration_changed_during_query_prevent_storage(self):
        original_key = self.key.read_bytes()
        for change in ("storage", "settings"):
            with self.subTest(change=change):
                self.key.write_bytes(original_key)
                self.settings_reader.return_value = SETTINGS
                client = FakeClient()

                def send(*args):
                    if change == "storage":
                        self.key.write_bytes(Fernet.generate_key())
                    else:
                        self.settings_reader.return_value = {
                            **SETTINGS,
                            "client_id": "fixture-changed-id",
                        }
                    return success()

                client.send.side_effect = send
                with self.assertRaises(sync.SyncError):
                    self.collect(client)
                self.assert_nothing_saved()

    def test_direct_client_reserves_shared_budget_and_uses_approval_operation(self):
        with (
            patch.object(sync, "configuration", return_value=SETTINGS),
            patch.object(sync, "issue_token", return_value="fixture-token"),
            patch.object(sync, "post_codef", return_value=success()) as post,
        ):
            self.collect(sync.Client(connect))
        self.assertEqual(post.call_args.args[2], "approval")
        self.assertEqual(self.fetch("SELECT requests FROM codef_budget")[0][0], 2)

    def test_key_rotation_after_normalization_discards_results_before_commit(self):
        normalize = direct.normalize

        def normalize_then_rotate(*args):
            items = normalize(*args)
            self.key.write_bytes(Fernet.generate_key())
            return items

        with (
            patch.object(direct, "normalize", side_effect=normalize_then_rotate),
            self.assertRaises(sync.SyncError),
        ):
            self.collect()
        self.assert_nothing_saved()

    def test_manual_status_and_review_are_scoped_to_current_owner_without_network_request(self):
        self.collect()
        self.collect(
            FakeClient(
                success([fixture_row(resCancelYN="2", resMemberStoreName="가상 다른 계정 가게")])
            ),
            owner=2,
            card="hyundai",
        )
        owner = app.test_client()
        partner = app.test_client()
        self.assertEqual(
            owner.post(
                "/api/login", json={"password": "fixture-owner-password"}, headers=HEADERS
            ).status_code,
            200,
        )
        self.assertEqual(
            partner.post(
                "/api/login",
                json={"username": "partner", "password": "fixture-partner-password"},
                headers=HEADERS,
            ).status_code,
            200,
        )
        with patch.object(sync, "post_codef") as post:
            own = owner.get("/api/codef-sync", headers=HEADERS).get_json()
            other = partner.get("/api/codef-sync", headers=HEADERS).get_json()
        post.assert_not_called()
        self.assertEqual(own["links"], [])
        self.assertEqual(other["links"], [])
        self.assertEqual([row["card"] for row in own["manual"]], ["lotte"])
        self.assertEqual([row["card"] for row in other["manual"]], ["hyundai"])
        self.assertEqual(own["manual"][0]["saved_count"], 1)
        self.assertEqual(other["manual"][0]["review_count"], 1)
        self.assertEqual(own["review"], [])
        self.assertEqual(len(other["review"]), 1)
        self.assertNotIn("가상 다른 계정 가게", str(own))
        self.assertEqual(
            set(own["manual"][0]),
            {"card", "last_success", "group_count", "saved_count", "review_count"},
        )
        self.assertNotIn("fixture-bank-id-private", str(own) + str(other))

    def test_cli_direct_never_registers_or_replaces_and_rejects_combined_flags(self):
        spec = importlib.util.spec_from_file_location(
            "connect_codef_direct_fixture",
            Path(__file__).resolve().parents[1] / "scripts/connect-codef-card.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        operator, payload = self.operator()
        args = ["connect-codef-card.py", "--card", "lotte", "--user", "owner", "--direct"]
        with (
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.object(cli.sys, "argv", args),
            patch.object(cli.runpy, "run_path", return_value=operator),
            patch.object(cli, "Client", return_value=FakeClient()),
            patch.object(cli, "register_account") as register,
            patch("builtins.print"),
        ):
            self.assertEqual(cli.main(), 0)
        register.assert_not_called()
        self.assertEqual(payload, {})
        self.assertEqual(len(self.fetch("SELECT * FROM codef_links")), 0)
        for suffix in (["--direct", "--replace"], ["--replace", "--direct"]):
            with (
                patch.object(cli.sys, "argv", args[:-1] + suffix),
                patch("builtins.print"),
                patch.object(cli, "Client") as client,
            ):
                self.assertEqual(cli.main(), 1)
                client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
