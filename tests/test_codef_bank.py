"""가상 우리은행으로 암호화·개인 권한·중복·중단 중 응답 폐기·카드 합계 분리를 검증합니다."""

from datetime import datetime, timedelta
import importlib.util
import os
from pathlib import Path
import tempfile
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

from app import app, connect
from auth import set_password
import codef_bank as bank
import codef_sync as sync

SETTINGS = dict(
    environment="demo",
    client_id="fixture-id",
    client_secret="fixture-secret",
    public_key="fixture-public",
)
ACCOUNT = "1002000000000"
HEADERS = {"X-Requested-With": "CardDashboard"}


def response(**changes):
    """실제 금융 값 대신 날짜가 유효한 가상 거래 한 건을 반환합니다."""
    row = dict(
        resAccountTrDate=datetime.now(bank.KST).strftime("%Y%m%d"),
        resAccountTrTime="120000",
        resAccountIn="0",
        resAccountOut="1200",
        resAfterTranBalance="5000",
        resAccountDesc1="가상 상대",
        resAccountDesc2="이체",
        resAccountDesc3="가상 메모",
    )
    row.update(changes)
    return dict(resAccount=ACCOUNT, resTrHistoryList=[row])


class FakeClient:
    def __init__(self, data):
        self.connect = connect
        self.credentials = Mock(return_value=(SETTINGS, "fixture-token"))
        self.send = Mock(return_value={"result": {"code": "CF-00000"}, "data": data})


class BankTests(unittest.TestCase):
    def setUp(self):
        self.old = dict(app.config)
        self.root = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.root.name) / "db.sqlite3")
        )
        self.key = Fernet.generate_key()
        path = Path(self.root.name) / "key"
        path.write_bytes(self.key)
        self.env = patch.dict(os.environ, {"MOA_CODEF_STORAGE_KEY_FILE": str(path)})
        self.env.start()
        self.config = patch.object(sync, "configuration", return_value=SETTINGS)
        self.settings_mock = self.config.start()
        set_password(connect, "fixture-owner-password")
        db = connect()
        with db:
            db.execute(
                "INSERT INTO users(id,username,password_hash,role) VALUES(2,'partner',?,'manager')",
                (generate_password_hash("fixture-partner-password"),),
            )
        db.close()
        self.fields = dict(
            account=ACCOUNT,
            connectedId="fixture-connected",
            accountPassword="fixture-RSA-only",
            birthDate="19900101",
        )
        self.start = (datetime.now(bank.KST) - timedelta(days=29)).strftime("%Y%m%d")
        self.end = datetime.now(bank.KST).strftime("%Y%m%d")
        self.items = bank.normalize(response(), ACCOUNT, self.start, self.end, self.key)
        self.id = bank.save_verified(connect, 1, self.fields, SETTINGS, self.key, self.items, None)

    def tearDown(self):
        self.config.stop()
        self.env.stop()
        app.config.update(self.old)
        self.root.cleanup()

    def rows(self, sql, args=()):
        db = connect()
        try:
            return [dict(row) for row in db.execute(sql, args)]
        finally:
            db.close()

    def link(self):
        return self.rows("SELECT * FROM bank_links WHERE id=?", (self.id,))[0]

    def execute(self, sql, args=()):
        db = connect()
        try:
            with db:
                db.execute(sql, args)
        finally:
            db.close()

    def login(self, partner=False):
        client = app.test_client()
        client.post(
            "/api/login",
            json={
                "username": "partner" if partner else "owner",
                "password": "fixture-partner-password" if partner else "fixture-owner-password",
            },
            headers=HEADERS,
        )
        return client

    def test_cipher_binding_and_personal_api_do_not_expose_auth(self):
        link = self.link()
        self.assertNotIn(ACCOUNT, str(link))
        self.assertNotIn("fixture-connected", str(link))
        self.assertEqual(bank.load_fields(link, self.key), self.fields)
        for name, value in [
            ("owner_id", 2),
            ("id", "other"),
            ("app_digest", "other"),
            ("account_tag", "other"),
        ]:
            with self.assertRaises(sync.SyncError):
                bank.load_fields(dict(link, **{name: value}), self.key)
        own = self.login().get("/api/bank")
        self.assertEqual(own.status_code, 200)
        self.assertEqual(len(own.json["transactions"]), 1)
        for secret in [
            ACCOUNT,
            "fixture-connected",
            "fixture-RSA-only",
            "900101",
            "cipher",
            "account_tag",
        ]:
            self.assertNotIn(secret, own.get_data(as_text=True))
        other = self.login(True).get("/api/bank")
        self.assertEqual(other.json["links"], [])
        self.assertEqual(other.json["transactions"], [])
        self.assertEqual(app.test_client().get("/api/bank").status_code, 401)

    def test_repeat_queries_do_not_change_card_ledger_or_duplicate_bank(self):
        client = FakeClient(response(resAccountDesc3="수정된 가상 메모"))
        for slot in ["a", "a", "b"]:
            bank.collect(connect, client, self.link(), slot)
        self.assertEqual(client.send.call_count, 2)
        self.assertEqual(client.send.call_args.args[2], "bank_transactions")
        payload = client.send.call_args.args[1]
        self.assertEqual(payload["withdrawAccountNo"], ACCOUNT)
        self.assertEqual(payload["withdrawAccountPassword"], "fixture-RSA-only")
        self.assertEqual(payload["birthDate"], "19900101")
        self.assertEqual(self.rows("SELECT * FROM transactions"), [])
        self.assertEqual(self.rows("SELECT * FROM imports"), [])
        rows = self.rows("SELECT * FROM bank_transactions")
        self.assertEqual(len(rows), 1)
        self.assertIn("수정된 가상 메모", rows[0]["description"])

    def test_invalid_row_rejects_whole_result(self):
        for changes in [
            dict(resAccountIn="abc"),
            dict(resAccountOut="-1"),
            dict(resAccountIn="1"),
            dict(resAccountTrTime="250000"),
            dict(resAccountTrDate="20000101"),
        ]:
            data = response()
            data["resTrHistoryList"].append(response(**changes)["resTrHistoryList"][0])
            with self.assertRaises(sync.SyncError):
                bank.normalize(data, ACCOUNT, self.start, self.end, self.key)
        with self.assertRaises(sync.SyncError):
            bank.normalize(
                dict(response(), resAccount="9999999999999"),
                ACCOUNT,
                self.start,
                self.end,
                self.key,
            )
        self.assertEqual(len(self.rows("SELECT * FROM bank_transactions")), 1)

    def test_zero_movement_rows_are_preserved_without_duplicate_or_card_expense(self):
        """입출금 0원 기록도 남기되 재조회 중복과 카드 소비 집계는 만들지 않습니다."""
        data = response(resAccountIn="0", resAccountOut="0", resAccountDesc1="가상 상태 기록")
        data["resTrHistoryList"].append(dict(data["resTrHistoryList"][0]))
        items = bank.normalize(data, ACCOUNT, self.start, self.end, self.key)
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0][0], items[1][0])
        self.assertTrue(all(item[3:5] == (0, 0) for item in items))
        client = FakeClient(data)
        for slot in ("zero-first", "zero-repeat"):
            bank.collect(connect, client, self.link(), slot)
        rows = self.rows("SELECT * FROM bank_transactions WHERE incoming=0 AND outgoing=0")
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.link()["state"], "ready")
        self.assertEqual(self.rows("SELECT * FROM transactions"), [])
        self.assertEqual(self.rows("SELECT * FROM imports"), [])
        invalid = dict(
            data,
            resTrHistoryList=[
                *data["resTrHistoryList"],
                response(resAccountIn="1")["resTrHistoryList"][0],
            ],
        )
        with self.assertRaises(sync.SyncError):
            bank.normalize(invalid, ACCOUNT, self.start, self.end, self.key)
        self.assertEqual(len(self.rows("SELECT * FROM bank_transactions")), 3)

    def test_network_result_is_discarded_after_pause_replace_or_user_revocation(self):
        for change in [
            "UPDATE bank_links SET enabled=0,version=version+1",
            "UPDATE bank_links SET version=version+1",
            "UPDATE users SET active=0 WHERE id=1",
        ]:
            self.execute("UPDATE bank_links SET enabled=1,state='ready'")
            self.execute("UPDATE users SET active=1 WHERE id=1")
            client = FakeClient(response(resAccountTrTime="130000"))
            original = client.send.return_value

            def mutate(*args):
                self.execute(change)
                return original

            client.send.side_effect = mutate
            bank.collect(connect, client, self.link(), change)
            self.assertEqual(len(self.rows("SELECT * FROM bank_transactions")), 1)
            self.assertEqual(
                self.rows("SELECT state FROM bank_jobs WHERE slot=?", (change,))[0]["state"],
                "discarded",
            )

    def test_configuration_change_during_network_stops_save(self):
        client = FakeClient(response(resAccountTrTime="130000"))

        def mutate(*args):
            self.settings_mock.return_value = dict(SETTINGS, client_id="changed")
            return {"result": {"code": "CF-00000"}, "data": response(resAccountTrTime="130000")}

        client.send.side_effect = mutate
        bank.collect(connect, client, self.link(), "key-change")
        self.assertEqual(self.link()["state"], "error")
        self.assertEqual(len(self.rows("SELECT * FROM bank_transactions")), 1)

    def test_auth_error_and_challenge_pause_without_repeating(self):
        client = FakeClient(response())
        client.send.return_value = {
            "result": {"code": "CF-12801", "message": "fixture-private-login"}
        }
        bank.collect(connect, client, self.link(), "auth-error")
        self.assertEqual(self.link()["state"], "needs_auth")
        bank.tick(connect, client)
        self.assertEqual(client.send.call_count, 1)
        self.assertNotIn("fixture-private-login", self.link()["message"])

    def test_shared_revision_and_owner_checks_for_pause(self):
        owner, other = self.login(), self.login(True)
        self.assertEqual(
            owner.post("/api/bank/pause", json={"id": self.id}, headers=HEADERS).status_code, 428
        )
        revision = other.get("/api/bank").headers["X-Moa-Revision"]
        self.assertEqual(
            other.post(
                "/api/bank/pause", json={"id": self.id}, headers={**HEADERS, "If-Match": revision}
            ).status_code,
            404,
        )
        revision = owner.get("/api/bank").headers["X-Moa-Revision"]
        self.assertEqual(
            owner.post(
                "/api/bank/pause", json={"id": self.id}, headers={**HEADERS, "If-Match": revision}
            ).status_code,
            200,
        )
        self.assertEqual(self.link()["enabled"], 0)
        self.assertEqual(len(self.rows("SELECT * FROM bank_transactions")), 1)
        self.assertEqual(
            owner.post("/api/bank/refresh", headers={**HEADERS, "If-Match": revision}).status_code,
            409,
        )

    def test_bank_registration_and_sms_validate_institution(self):
        data = dict(
            connectedId="fixture-id",
            successList=[
                dict(organization="0020", businessType="BK", clientType="P", code="CF-00000")
            ],
            errorList=[],
        )
        self.assertEqual(bank.registration_id(data), "fixture-id")
        with self.assertRaises(sync.SyncError):
            bank.registration_id(
                dict(data, successList=[dict(data["successList"][0], businessType="CD")])
            )
        challenge = dict(
            continue2Way=True,
            method="smsAuthNo",
            jobIndex=0,
            threadIndex=0,
            jti="fixture-jti",
            twoWayTimestamp=123,
        )
        client = FakeClient({})
        client.send.side_effect = [
            {
                "result": {"code": "CF-00000"},
                "data": {
                    "errorList": [
                        dict(challenge, organization="0020", businessType="BK", code="CF-03002")
                    ]
                },
            },
            {"result": {"code": "CF-00000"}, "data": data},
        ]
        result = bank.query(
            client, "fixture-token", {"accountList": []}, "register", lambda method: "123456"
        )
        self.assertEqual(result, data)
        self.assertEqual(client.send.call_count, 2)
        self.assertEqual(client.send.call_args.args[1]["twoWayInfo"]["jobIndex"], 0)

    def test_cli_cancellation_and_validation_send_no_financial_request(self):
        spec = importlib.util.spec_from_file_location(
            "bank_cli", Path(__file__).resolve().parents[1] / "scripts/connect-codef-bank.py"
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        for answers in [
            [""],
            ["연결", "fixture-login", "fixture-password", "900101"],
            ["연결", "fixture-login", "fixture-password", "19900132"],
            ["연결", "fixture-login", "fixture-password", "19900101", ACCOUNT, "12"],
            ["연결", "fixture-login", "fixture-password", "19900101", "invalid-account", "1234"],
        ]:
            with (
                patch.object(cli.sys, "argv", ["connect-codef-bank.py", "--user", "owner"]),
                patch.object(cli.sys.stdin, "isatty", return_value=True),
                patch.object(
                    cli.runpy, "run_path", return_value={"read_hidden": Mock(side_effect=answers)}
                ),
                patch.object(cli, "Client") as client,
                patch("builtins.print") as output,
            ):
                cli.main()
                client.assert_not_called()
                printed = str(output.call_args_list)
                self.assertNotIn("fixture-password", printed)
                self.assertNotIn(ACCOUNT, printed)

    def test_registration_details_are_safe_and_never_retry(self):
        """이번 등록 실패처럼 CF-04000 안의 오류를 보이되 원문과 다른 기관 정보는 버립니다."""
        entry = dict(
            organization="0020",
            businessType="BK",
            clientType="P",
            countryCode="KR",
            code="CF-12401",
            extraMessage="accountPassword: fixture-private-pin",
            userError="fixture-private-counter",
        )
        client = FakeClient({})
        client.send.return_value = {
            "result": {"code": "CF-04000", "message": "fixture-private-login"},
            "data": {"successList": [], "errorList": [entry]},
        }
        with self.assertRaises(bank.CardCheckError) as raised:
            bank.query(client, "fixture-token", {"accountList": []}, "register", Mock())
        self.assertEqual(client.send.call_count, 1)
        message = str(raised.exception)
        self.assertIn("기관 상세 코드: CF-12401", message)
        self.assertIn("accountPassword", message)
        self.assertNotIn("fixture-private", message)
        for errors, success in [
            ([dict(entry, organization="0309")], []),
            ([dict(entry, businessType="CD")], []),
            ([dict(entry, clientType="B")], []),
            ([dict(entry, countryCode="OTHER")], []),
            ([dict(entry, code="fixture-private")], []),
            ([entry, entry], []),
            ([entry], [{"code": "CF-00000"}]),
        ]:
            self.assertEqual(
                bank.registration_hint({"errorList": errors, "successList": success}), ""
            )

    def test_cli_registration_failure_preserves_connection_without_retry(self):
        """은행 로그인 필수 계좌값까지 한 번 전송하고 실패 시 재요청·저장을 하지 않습니다."""
        spec = importlib.util.spec_from_file_location(
            "bank_cli_diagnostic",
            Path(__file__).resolve().parents[1] / "scripts/connect-codef-bank.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        hidden = Mock(
            side_effect=["연결", "fixture-login", "fixture-password", "19900101", ACCOUNT, "1234"]
        )
        client = FakeClient(
            {
                "errorList": [
                    dict(organization="0020", businessType="BK", clientType="P", code="CF-12800")
                ],
                "successList": [],
            }
        )
        client.send.return_value["result"]["code"] = "CF-04000"
        with (
            patch.object(cli.sys, "argv", ["connect-codef-bank.py", "--user", "owner"]),
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.object(
                cli.runpy,
                "run_path",
                return_value={"read_hidden": hidden, "answer_challenge": Mock()},
            ),
            patch.object(cli, "Client", return_value=client),
            patch.object(
                cli, "encrypt_password", side_effect=["fixture-RSA-login", "fixture-RSA-pin"]
            ),
            patch("builtins.print") as output,
        ):
            self.assertEqual(cli.main(), 2)
        self.assertEqual(hidden.call_count, 6)
        self.assertEqual(client.send.call_count, 1)
        self.assertEqual(self.link()["version"], 1)
        self.assertIn("CF-12800", str(output.call_args_list))
        self.assertNotIn("fixture-login", str(output.call_args_list))
        self.assertNotIn("fixture-password", str(output.call_args_list))
        self.assertNotIn("1234", str(output.call_args_list))
        self.assertNotIn(ACCOUNT, str(output.call_args_list))

    def test_cli_success_verifies_account_before_replacing_connection(self):
        """성공 경로도 가상 입력으로 끝까지 실행해 로그인/PIN 평문이 저장되지 않음을 확인합니다."""
        spec = importlib.util.spec_from_file_location(
            "bank_cli_success",
            Path(__file__).resolve().parents[1] / "scripts/connect-codef-bank.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        hidden = Mock(
            side_effect=["연결", "fixture-login", "fixture-password", "19900101", ACCOUNT, "1234"]
        )
        client = FakeClient({})
        responses = iter(
            [
                {
                    "result": {"code": "CF-00000"},
                    "data": dict(
                        connectedId="fixture-new-connected",
                        successList=[
                            dict(
                                organization="0020",
                                businessType="BK",
                                clientType="P",
                                code="CF-00000",
                            )
                        ],
                        errorList=[],
                    ),
                },
                {"result": {"code": "CF-00000"}, "data": response()},
            ]
        )
        requests = []

        def send(token, body, operation):
            # CLI가 등록 후 메모리의 entry를 지워도 실제 전송 당시 필수 항목을 검증합니다.
            import json

            requests.append((operation, json.loads(json.dumps(body))))
            return next(responses)

        client.send.side_effect = send
        with (
            patch.object(cli.sys, "argv", ["connect-codef-bank.py", "--user", "owner"]),
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.object(
                cli.runpy,
                "run_path",
                return_value={"read_hidden": hidden, "answer_challenge": Mock()},
            ),
            patch.object(cli, "Client", return_value=client),
            patch.object(
                cli, "encrypt_password", side_effect=["fixture-RSA-login", "fixture-RSA-pin"]
            ),
            patch("builtins.print") as output,
        ):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(client.send.call_count, 2)
        self.assertEqual(
            [call.args[2] for call in client.send.call_args_list], ["register", "bank_transactions"]
        )
        entry = requests[0][1]["accountList"][0]
        self.assertEqual(entry["organization"], "0020")
        self.assertEqual(entry["businessType"], "BK")
        self.assertEqual(entry["loginType"], "1")
        self.assertEqual(entry["birthDate"], "19900101")
        self.assertEqual(entry["withdrawAccountNo"], ACCOUNT)
        self.assertEqual(entry["withdrawAccountPassword"], "fixture-RSA-pin")
        self.assertEqual(entry["password"], "fixture-RSA-login")
        self.assertEqual(requests[1][1]["withdrawAccountNo"], ACCOUNT)
        self.assertEqual(requests[1][1]["withdrawAccountPassword"], "fixture-RSA-pin")
        self.assertEqual(requests[1][1]["accountPassword"], "fixture-RSA-pin")
        self.assertEqual(requests[1][1]["birthDate"], "19900101")
        self.assertNotIn("id", requests[1][1])
        self.assertNotIn("password", requests[1][1])
        self.assertEqual(self.link()["version"], 2)
        fields = bank.load_fields(self.link(), self.key)
        self.assertEqual(fields["connectedId"], "fixture-new-connected")
        self.assertEqual(fields["accountPassword"], "fixture-RSA-pin")
        self.assertEqual(len(self.rows("SELECT * FROM bank_transactions")), 1)
        printed = str(output.call_args_list)
        for secret in ["fixture-login", "fixture-password", "1234", ACCOUNT]:
            self.assertNotIn(secret, printed)

    def test_documented_error_symbols_never_expose_arbitrary_values(self):
        """문서의 reqUserPass 기호와 알려진 키만 고정 이름으로 표시합니다."""
        from codef_card import missing_parameter_hint

        for code in ("CF-12401", "CF-12411"):
            self.assertIn(
                "password", missing_parameter_hint(dict(code=code, userError="reqUserPass"))
            )
            self.assertIn(
                "withdrawAccountNo",
                missing_parameter_hint(dict(code=code, errorUser="withdrawAccountNo")),
            )
            for private in ("fixture-private", "01", "99", {"password": "fixture-private"}):
                hint = missing_parameter_hint(dict(code=code, userError=private))
                self.assertNotIn("fixture-private", hint)
                self.assertIn("확인 불가", hint)
        self.assertNotIn(
            "password", missing_parameter_hint(dict(code="CF-12801", userError="reqUserPass"))
        )

    def test_legacy_six_digit_birth_date_stops_before_bank_request(self):
        """과거 형식이 암호화 연결에 남아 있어도 잘못된 로그인 요청을 반복하지 않습니다."""
        import json

        link = self.link()
        value = json.loads(Fernet(self.key).decrypt(link["cipher"].encode()))
        value["birthDate"] = "900101"
        cipher = Fernet(self.key).encrypt(json.dumps(value).encode()).decode()
        self.execute("UPDATE bank_links SET cipher=? WHERE id=?", (cipher, self.id))
        client = FakeClient(response())
        bank.collect(connect, client, self.link(), "invalid-legacy-birth")
        client.send.assert_not_called()
        self.assertEqual(self.link()["state"], "error")
        self.assertEqual(len(self.rows("SELECT * FROM bank_transactions")), 1)

    def test_network_failure_pauses_worker_without_auth_retry(self):
        """CF-12200은 통신 오류로 구분하고 작업자가 같은 연결을 계속 요청하지 않습니다."""
        client = FakeClient({})
        client.send.return_value = {
            "result": {"code": "CF-12200", "message": "fixture-private-host"}
        }
        bank.collect(connect, client, self.link(), "network-error")
        self.assertEqual(self.link()["state"], "error")
        self.assertIn("통신 연결 실패", self.link()["message"])
        self.assertNotIn("fixture-private-host", self.link()["message"])
        bank.tick(connect, client)
        self.assertEqual(client.send.call_count, 1)

    def test_page_change_is_technical_error_and_preserves_saved_bank_rows(self):
        """CF-12701은 인증 재입력으로 안내하지 않고 한 번 조회 뒤 멈춥니다."""
        client = FakeClient({})
        client.send.return_value = {
            "result": {"code": "CF-12701", "message": "fixture-private-page-detail"}
        }
        bank.collect(connect, client, self.link(), "page-change")
        self.assertEqual(self.link()["state"], "error")
        self.assertIn("전문·페이지 변경", self.link()["message"])
        self.assertNotIn("fixture-private", self.link()["message"])
        self.assertEqual(len(self.rows("SELECT * FROM bank_transactions")), 1)
        bank.tick(connect, client)
        self.assertEqual(client.send.call_count, 1)

    def test_cli_network_retry_requires_one_explicit_choice(self):
        """등록이 끝난 조회에서만 사용자가 선택한 1회 재요청을 허용합니다."""
        spec = importlib.util.spec_from_file_location(
            "bank_cli_network",
            Path(__file__).resolve().parents[1] / "scripts/connect-codef-bank.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        payload = bank.transaction_payload(self.fields, self.start, self.end)
        network = {"result": {"code": "CF-12200", "message": "fixture-private-host"}}
        success = {"result": {"code": "CF-00000"}, "data": response()}
        authentication = {"result": {"code": "CF-12801", "message": "fixture-private-login"}}
        for choice, replies, calls, succeeds in [
            ("", [network], 1, False),
            ("2", [network], 1, False),
            ("1", [network, success], 2, True),
            ("1", [network, network], 2, False),
            ("1", [authentication], 1, False),
        ]:
            with self.subTest(choice=choice, calls=calls, succeeds=succeeds):
                client = FakeClient({})
                client.send.side_effect = replies
                operator = {"read_hidden": Mock(return_value=choice), "answer_challenge": Mock()}
                with patch("builtins.print") as output:
                    if succeeds:
                        self.assertEqual(
                            cli.query_transactions(client, "fixture-token", payload, operator),
                            response(),
                        )
                    else:
                        with self.assertRaises(bank.CardCheckError):
                            cli.query_transactions(client, "fixture-token", payload, operator)
                self.assertEqual(client.send.call_count, calls)
                self.assertEqual(
                    operator["read_hidden"].call_count, 0 if replies[0] is authentication else 1
                )
                for call in client.send.call_args_list:
                    self.assertEqual(call.args[1], payload)
                    self.assertEqual(call.args[2], "bank_transactions")
                printed = str(output.call_args_list)
                for private in ["fixture-private", "fixture-token", ACCOUNT, "fixture-RSA-only"]:
                    self.assertNotIn(private, printed)
                operator["answer_challenge"].assert_not_called()

    def test_budget_preflight_stops_before_private_input_and_oauth(self):
        """잔여 예산 0~2회에는 로그인 입력/토큰/등록부터 받지 않습니다."""
        spec = importlib.util.spec_from_file_location(
            "bank_cli_budget", Path(__file__).resolve().parents[1] / "scripts/connect-codef-bank.py"
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        day = datetime.now(bank.KST).strftime("%Y-%m-%d")
        for used in (58, 59, 60):
            self.execute(
                "INSERT INTO codef_budget VALUES(?,?) ON CONFLICT(day) DO UPDATE SET requests=excluded.requests",
                (day, used),
            )
            with (
                patch.object(cli.sys, "argv", ["connect-codef-bank.py", "--user", "owner"]),
                patch.object(cli.sys.stdin, "isatty", return_value=True),
                patch.object(cli.runpy, "run_path") as operator,
                patch.object(cli, "Client") as client,
                patch("builtins.print"),
            ):
                self.assertEqual(cli.main(), 2)
                operator.assert_not_called()
                client.assert_not_called()
            self.assertEqual(
                self.rows("SELECT requests FROM codef_budget WHERE day=?", (day,))[0]["requests"],
                used,
            )

    def test_retry_choice_is_not_offered_when_no_request_budget_remains(self):
        """통신 실패 뒤 예산이 없으면 추가 선택이나 금융 요청 없이 중단합니다."""
        spec = importlib.util.spec_from_file_location(
            "bank_cli_retry_budget",
            Path(__file__).resolve().parents[1] / "scripts/connect-codef-bank.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        self.execute(
            "INSERT INTO codef_budget VALUES(?,60)", (datetime.now(bank.KST).strftime("%Y-%m-%d"),)
        )
        client = FakeClient({})
        client.send.return_value = {"result": {"code": "CF-12200"}}
        operator = {"read_hidden": Mock(return_value="1"), "answer_challenge": Mock()}
        with patch("builtins.print"), self.assertRaises(sync.BudgetError):
            cli.query_transactions(
                client,
                "fixture-token",
                bank.transaction_payload(self.fields, self.start, self.end),
                operator,
            )
        self.assertEqual(client.send.call_count, 1)
        operator["read_hidden"].assert_not_called()

    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_mobile_bank_screen_and_direct_navigation(self):
        """가상 입출금의 모바일 줄바꿈·딥링크·중지 버튼과 캐시 미사용을 확인합니다."""
        from werkzeug.serving import make_server, WSGIRequestHandler

        data = response(resAccountIn="0", resAccountOut="0", resAccountDesc1="가상 상태 기록")
        bank.collect(connect, FakeClient(data), self.link(), "zero-browser")

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                ["node", "tests/bank-ui.cjs"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
                check=True,
                timeout=75,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
