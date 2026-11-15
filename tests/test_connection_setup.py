"""가상 기관으로 휴대폰 인증의 개인 권한·추가 인증·취소·암호화·동시성을 검증합니다."""

import copy
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from werkzeug.security import generate_password_hash

from app import app, connect
from auth import set_password
import connection_setup as setup
import codef_bank as bank
import codef_card_auth as card_auth
import codef_sync as sync
from test_codef_sync import approval
from test_codef_bank import ACCOUNT, response as bank_response

HEADERS = {"X-Requested-With": "CardDashboard"}


class ConnectionSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.settings = dict(
            environment="demo",
            client_id="fixture-client",
            client_secret="fixture-secret",
            public_key=key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
            .strip(),
        )

    def setUp(self):
        self.old = dict(app.config)
        self.root = tempfile.TemporaryDirectory()
        root = Path(self.root.name)
        self.key = Fernet.generate_key()
        (root / "key").write_bytes(self.key)
        (root / "settings.json").write_text(json.dumps(self.settings))
        self.env = patch.dict(
            os.environ,
            {
                "MOA_CODEF_STORAGE_KEY_FILE": str(root / "key"),
                "MOA_CODEF_CONFIG_FILE": str(root / "settings.json"),
                "MOA_CODEF_DAILY_REQUEST_LIMIT": "60",
            },
        )
        self.env.start()
        app.config.update(TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(root / "data.sqlite3"))
        set_password(connect, "fixture-owner-password")
        self.execute(
            "INSERT INTO users(id,username,password_hash,role) VALUES(2,'partner',?,'manager')",
            (generate_password_hash("fixture-partner-password"),),
        )
        sync.save_link(
            connect, 1, "samsung", "fixture-owner-connected", self.settings, verifying=False
        )
        self.original = self.rows("SELECT * FROM codef_links WHERE owner_id=1")
        self.owner = self.login()
        self.partner = self.login(partner=True)
        self.calls = []
        self.releases = []
        self.handler = self.default_send
        self.token = patch.object(sync, "issue_token", return_value="fixture-token")
        self.sender = patch.object(sync, "post_codef", side_effect=self.send)
        self.token.start()
        self.sender.start()
        with setup.registry_lock:
            setup.registry.clear()

    def tearDown(self):
        for event in self.releases:
            event.set()
        for job in list(setup.registry.values()):
            with job.condition:
                if job.state in ("working", "needs_input"):
                    job.state = "cancelled"
                    job.condition.notify_all()
            if job.thread.ident is not None:
                job.thread.join(timeout=8)
            self.assertFalse(job.thread.is_alive())
        setup.registry.clear()
        self.sender.stop()
        self.token.stop()
        self.env.stop()
        app.config.update(self.old)
        self.root.cleanup()

    def rows(self, sql, args=()):
        db = connect()
        try:
            return [dict(row) for row in db.execute(sql, args)]
        finally:
            db.close()

    def execute(self, sql, args=()):
        db = connect()
        try:
            with db:
                db.execute(sql, args)
        finally:
            db.close()

    def login(self, partner=False):
        client = app.test_client()
        result = client.post(
            "/api/login",
            json={
                "username": "partner" if partner else "owner",
                "password": "fixture-partner-password" if partner else "fixture-owner-password",
            },
            headers=HEADERS,
        )
        self.assertEqual(result.status_code, 200)
        return client

    def send(self, token, payload, operation="approval"):
        self.calls.append((operation, copy.deepcopy(payload)))
        return self.handler(token, payload, operation)

    def default_send(self, token, payload, operation):
        if operation == "register":
            entry = payload["accountList"][0]
            return dict(
                result={"code": "CF-00000"},
                data=dict(
                    connectedId="fixture-new-connected",
                    errorList=[],
                    successList=[
                        dict(
                            organization=entry["organization"],
                            businessType=entry["businessType"],
                            clientType="P",
                            code="CF-00000",
                        )
                    ],
                ),
            )
        return dict(
            result={"code": "CF-00000"},
            data=bank_response() if operation == "bank_transactions" else [approval()],
        )

    def payload(self, kind="card", provider="samsung", **changes):
        data = dict(
            nonce=uuid.uuid4().hex,
            kind=kind,
            provider=provider,
            consent=True,
            login_id="fixture-web-login",
            password="fixture-web-password",
        )
        if kind == "bank":
            data.update(account=ACCOUNT, pin="0000", birth_date="19900101")
        elif provider == "hyundai":
            data.update(card_number="0" * 16, pin="0000")
        data.update(changes)
        return data

    def start(self, payload=None, client=None, headers=None):
        client = client or self.partner
        version = client.get("/api/connections").headers["X-Moa-Revision"]
        return client.post(
            "/api/connections/setup",
            json=payload or self.payload(),
            headers=headers or {**HEADERS, "If-Match": version},
        )

    def wait(self, identifier, states=("success", "error", "cancelled", "expired"), client=None):
        client = client or self.partner
        until = time.time() + 8
        while time.time() < until:
            response = client.get("/api/connections/setup/" + identifier)
            self.assertEqual(response.status_code, 200)
            if response.json["state"] in states:
                return response.json
            time.sleep(0.02)
        self.fail("Fixture setup did not finish")

    def gate_query(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def send(token, payload, operation):
            if operation != "register":
                entered.set()
                release.wait(timeout=5)
            return self.default_send(token, payload, operation)

        self.handler = send
        return entered, release

    def test_manager_connects_to_own_account_without_changing_owner_connection(self):
        start = self.start()
        self.assertEqual(start.status_code, 202)
        self.assertEqual(self.wait(start.json["id"])["state"], "success")
        self.assertEqual(self.rows("SELECT * FROM codef_links WHERE owner_id=1"), self.original)
        records = self.partner.get("/api/data").json["transactions"]
        self.assertEqual(len(records), 1)
        self.assertEqual(self.owner.get("/api/data").json["transactions"], [])
        self.assertEqual([c[0] for c in self.calls], ["register", "approval"])
        self.assertNotIn("fixture-web-password", str(self.calls))
        self.assertNotIn("fixture-web-login", str(self.rows("SELECT * FROM connection_setups")))
        meta = self.partner.get("/api/connections")
        self.assertEqual(meta.headers["Cache-Control"], "no-store")
        self.assertNotIn("fixture-new-connected", meta.get_data(as_text=True))

    def test_bank_is_encrypted_scoped_and_does_not_add_consumption(self):
        start = self.start(self.payload("bank", "woori"))
        self.assertEqual(self.wait(start.json["id"])["state"], "success")
        link = self.rows("SELECT * FROM bank_links WHERE owner_id=2")[0]
        self.assertEqual(bank.load_fields(link, self.key)["account"], ACCOUNT)
        self.assertNotIn(ACCOUNT, str(link))
        self.assertNotIn("19900101", str(link))
        entry = self.calls[0][1]["accountList"][0]
        self.assertEqual(entry["withdrawAccountNo"], ACCOUNT)
        self.assertNotEqual(entry["withdrawAccountPassword"], "0000")
        self.assertEqual(len(self.partner.get("/api/bank").json["transactions"]), 1)
        self.assertEqual(self.owner.get("/api/bank").json["transactions"], [])
        self.assertEqual(self.partner.get("/api/data").json["transactions"], [])

    def test_hyundai_reuse_and_encrypted_database_binding_are_verified_before_save(self):
        start = self.start(self.payload(provider="hyundai"))
        self.assertEqual(self.wait(start.json["id"])["state"], "success")
        self.assertEqual(
            [op for op, _ in self.calls], ["register", "approval", "approval", "approval"]
        )
        ciphers = [body["cardPassword"] for op, body in self.calls if op == "approval"]
        self.assertEqual(len(set(ciphers)), 1)
        link = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='hyundai'")[0]
        self.assertTrue(link["card_auth_cipher"])
        self.assertEqual(link["card_auth_ref"], "")
        self.assertNotIn("0" * 16, link["card_auth_cipher"])
        self.assertNotIn(ciphers[0], link["card_auth_cipher"])
        fields, snapshot = card_auth.load(link, "fixture-new-connected", self.settings, self.key)
        self.assertEqual(fields["cardPassword"], ciphers[0])
        for modified, cid, settings in [
            ({**link, "owner_id": 1}, "fixture-new-connected", self.settings),
            (link, "fixture-other", self.settings),
            (link, "fixture-new-connected", {**self.settings, "client_id": "other-fixture"}),
        ]:
            with self.assertRaises(card_auth.CardAuthError):
                card_auth.load(modified, cid, settings, self.key)
        with self.assertRaises(card_auth.CardAuthError):
            card_auth.check_unchanged({**link, "card_auth_cipher": ""}, snapshot)
        # 실제 작업자와 같은 경로가 별도 비밀 볼륨 쓰기 없이 암호문을 읽습니다.
        sync.collect(connect, sync.Client(connect), link, "fixture-follow-up")
        latest = self.rows("SELECT * FROM codef_links WHERE owner_id=2")[0]
        self.assertEqual(latest["state"], "ready")
        self.assertEqual(len(self.partner.get("/api/data").json["transactions"]), 1)
        exposed = self.partner.get("/api/codef-sync").get_data(as_text=True)
        for secret in ("card_auth_cipher", link["card_auth_cipher"], ciphers[0], "0" * 16):
            self.assertNotIn(secret, exposed)

    def test_failed_login_is_not_retried_or_exposed(self):
        self.handler = lambda *args: dict(
            result={"code": "CF-12800", "message": "fixture-private-login-value"}
        )
        start = self.start()
        result = self.wait(start.json["id"])
        self.assertEqual(result["state"], "error")
        self.assertNotIn("fixture-private-login-value", json.dumps(result))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.rows("SELECT * FROM codef_links WHERE owner_id=2"), [])

    def test_partial_or_wrong_registration_never_replaces_existing_connection(self):
        def send(token, payload, operation):
            result = self.default_send(token, payload, operation)
            result["data"]["successList"][0]["organization"] = "0309"
            return result

        self.handler = send
        start = self.start(client=self.owner)
        self.assertEqual(self.wait(start.json["id"], client=self.owner)["state"], "error")
        self.assertEqual(self.rows("SELECT * FROM codef_links WHERE owner_id=1"), self.original)
        self.assertEqual(len(self.calls), 1)

    def test_cancel_in_flight_discards_reply_and_does_not_hold_database_lock(self):
        entered, release = self.gate_query()
        start = self.start()
        self.assertTrue(entered.wait(3))
        self.execute("INSERT INTO spending_categories VALUES(1,'custom:fixture','가상 분야',1)")
        self.assertEqual(
            self.owner.get("/api/connections/setup/" + start.json["id"]).status_code, 404
        )
        cancelled = self.partner.post(
            "/api/connections/setup/" + start.json["id"] + "/cancel", headers=HEADERS
        )
        self.assertEqual(cancelled.json["state"], "cancelled")
        release.set()
        setup.registry[start.json["id"]].thread.join(3)
        self.assertEqual(self.rows("SELECT * FROM codef_links WHERE owner_id=2"), [])
        self.assertEqual(self.partner.get("/api/data").json["transactions"], [])

    def test_logout_before_reply_does_not_store_new_connection(self):
        entered, release = self.gate_query()
        start = self.start()
        self.assertTrue(entered.wait(3))
        self.assertEqual(self.partner.post("/api/logout", headers=HEADERS).status_code, 200)
        release.set()
        setup.registry[start.json["id"]].thread.join(3)
        self.assertEqual(self.rows("SELECT * FROM codef_links WHERE owner_id=2"), [])

    def test_revoked_account_and_changed_revision_discard_late_results(self):
        for statement in (
            "UPDATE users SET active=0 WHERE id=2",
            "INSERT INTO personal_revisions VALUES(2,1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1",
        ):
            with self.subTest(statement=statement):
                entered, release = self.gate_query()
                self.execute("UPDATE users SET active=1 WHERE id=2")
                self.execute("DELETE FROM connection_setups")
                start = self.start()
                self.assertTrue(entered.wait(3))
                self.execute(statement)
                release.set()
                setup.registry[start.json["id"]].thread.join(3)
                self.assertEqual(self.rows("SELECT * FROM codef_links WHERE owner_id=2"), [])

    def test_required_consent_revision_origin_and_target_validation_prevent_requests(self):
        for payload in [
            self.payload(consent=False),
            self.payload(owner_id=1),
            self.payload(provider="unsupported"),
            self.payload("bank", "woori", birth_date="900101"),
            self.payload(provider="hyundai", pin="12"),
        ]:
            self.assertEqual(self.start(payload).status_code, 400)
        self.assertEqual(self.start(headers=HEADERS).status_code, 409)
        version = self.partner.get("/api/connections").headers["X-Moa-Revision"]
        self.assertEqual(self.start(headers={"If-Match": version}).status_code, 403)
        self.assertEqual(
            self.start(
                headers={**HEADERS, "If-Match": version, "Origin": "https://example.invalid"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.start(headers={**HEADERS, "If-Match": version, "X-Moa-User": "1"}).status_code, 409
        )
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows("SELECT * FROM connection_setups"), [])
        self.assertEqual(app.test_client().get("/api/connections").status_code, 401)

    def test_budget_block_happens_before_creating_setup_or_sending_credentials(self):
        self.execute(
            "INSERT INTO codef_budget VALUES(?,59)", (datetime.now(sync.KST).date().isoformat(),)
        )
        result = self.start()
        self.assertEqual(result.status_code, 429)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows("SELECT * FROM connection_setups"), [])

    def test_duplicate_request_and_provider_cooldown_do_not_resend(self):
        payload = self.payload()
        first = self.start(payload)
        self.assertEqual(self.wait(first.json["id"])["state"], "success")
        count = len(self.calls)
        repeated = self.start(payload)
        self.assertEqual(repeated.json["id"], first.json["id"])
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.start().status_code, 429)
        self.assertEqual(self.start({**payload, "provider": "shinhan"}).status_code, 409)

    def test_other_sessions_cannot_read_answer_or_cancel_my_setup(self):
        entered, release = self.gate_query()
        first = self.start()
        self.assertTrue(entered.wait(3))
        other = self.login(partner=True)
        for client in (self.owner, other):
            route = "/api/connections/setup/" + first.json["id"]
            self.assertEqual(client.get(route).status_code, 404)
            self.assertEqual(
                client.post(route + "/answer", json={"answer": "1"}, headers=HEADERS).status_code,
                404,
            )
            self.assertEqual(client.post(route + "/cancel", headers=HEADERS).status_code, 404)
        release.set()
        self.assertEqual(self.wait(first.json["id"])["state"], "success")

    def test_additional_auth_only_continues_after_current_challenge_answer(self):
        for provider, method, value in [
            ("samsung", "smsAuthNo", "123456"),
            ("shinhan", "simpleAuth", "1"),
            ("woori", "emailAuthNo", "fixture123"),
        ]:
            with self.subTest(method=method):

                def send(token, payload, operation):
                    if operation == "register" and not payload.get("is2Way"):
                        return dict(
                            result={"code": "CF-03002"},
                            data=dict(
                                continue2Way=True,
                                method=method,
                                jobIndex=1,
                                threadIndex=1,
                                jti="fixture-session",
                                twoWayTimestamp=1,
                            ),
                        )
                    return self.default_send(token, payload, operation)

                self.handler = send
                first = self.start(self.payload(provider=provider))
                challenge = self.wait(first.json["id"], states=("needs_input",))
                self.assertEqual(challenge["method"], method)
                count = len(self.calls)
                time.sleep(0.05)
                self.assertEqual(len(self.calls), count)
                route = "/api/connections/setup/" + first.json["id"] + "/answer"
                self.assertEqual(
                    self.partner.post(
                        route, json={"challenge_id": "old", "answer": value}, headers=HEADERS
                    ).status_code,
                    409,
                )
                response = self.partner.post(
                    route,
                    json={"challenge_id": challenge["challenge_id"], "answer": value},
                    headers=HEADERS,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.wait(first.json["id"])["state"], "success")
                self.assertEqual(
                    self.partner.post(
                        route,
                        json={"challenge_id": challenge["challenge_id"], "answer": value},
                        headers=HEADERS,
                    ).status_code,
                    409,
                )
                if method != "simpleAuth":
                    self.assertNotIn(value, str(self.rows("SELECT * FROM connection_setups")))

    def test_kb_possession_is_temporary_and_connection_only_query_must_succeed(self):
        def send(token, payload, operation):
            if (
                operation == "approval"
                and len([op for op, _ in self.calls if op == "approval"]) == 1
            ):
                return dict(result={"code": "CF-12108"})
            return self.default_send(token, payload, operation)

        self.handler = send
        start = self.start(self.payload(provider="kb"))
        challenge = self.wait(start.json["id"], states=("needs_input",))
        self.assertEqual(challenge["method"], "kb_possession")
        answer = dict(
            challenge_id=challenge["challenge_id"], answer={"number": "0" * 16, "pin": "00"}
        )
        route = "/api/connections/setup/" + start.json["id"] + "/answer"
        self.assertEqual(
            self.partner.post(
                route,
                json={**answer, "answer": {"number": "0" * 16, "pin": "0000"}},
                headers=HEADERS,
            ).status_code,
            400,
        )
        self.assertEqual(self.partner.post(route, json=answer, headers=HEADERS).status_code, 200)
        self.assertEqual(self.wait(start.json["id"])["state"], "success")
        approvals = [body for op, body in self.calls if op == "approval"]
        self.assertEqual(len(approvals), 3)
        self.assertNotIn("cardPassword", approvals[0])
        self.assertIn("cardPassword", approvals[1])
        self.assertNotIn("cardPassword", approvals[2])
        link = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertEqual(link["card_auth_cipher"], "")
        self.assertNotIn("0" * 16, str(link))

    def connect_kb_with_saved_possession(self):
        """하루 첫 조회처럼 CF-12108을 받은 KB 연결에서 저장을 골라 연결합니다."""

        def send(token, payload, operation):
            if (
                operation == "approval"
                and len([op for op, _ in self.calls if op == "approval"]) == 1
            ):
                return dict(result={"code": "CF-12108"})
            return self.default_send(token, payload, operation)

        self.handler = send
        start = self.start(self.payload(provider="kb"))
        challenge = self.wait(start.json["id"], states=("needs_input",))
        answer = {"number": "0" * 16, "pin": "00", "remember": True}
        route = "/api/connections/setup/" + start.json["id"] + "/answer"
        bad = {**answer, "remember": "yes"}
        self.assertEqual(
            self.partner.post(
                route,
                json=dict(challenge_id=challenge["challenge_id"], answer=bad),
                headers=HEADERS,
            ).status_code,
            400,
        )
        self.assertEqual(
            self.partner.post(
                route,
                json=dict(challenge_id=challenge["challenge_id"], answer=answer),
                headers=HEADERS,
            ).status_code,
            200,
        )
        self.assertEqual(self.wait(start.json["id"])["state"], "success")
        return self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]

    def test_kb_possession_saved_on_request_and_used_only_after_cf_12108(self):
        link = self.connect_kb_with_saved_possession()
        sent = [body for op, body in self.calls if op == "approval"][1]["cardPassword"]
        self.assertTrue(link["card_auth_cipher"])
        self.assertNotIn("0" * 16, link["card_auth_cipher"])
        self.assertNotIn(sent, link["card_auth_cipher"])
        fields, _ = card_auth.load(link, "fixture-new-connected", self.settings, self.key)
        self.assertEqual((fields["cardNo"], fields["cardPassword"]), ("0" * 16, sent))
        # 현대용 암호문으로 바꿔 끼우거나 다른 사용자에게 붙이면 읽지 않습니다.
        for modified in ({**link, "card": "hyundai"}, {**link, "owner_id": 1}):
            with self.assertRaises(card_auth.CardAuthError):
                card_auth.load(modified, "fixture-new-connected", self.settings, self.key)

        # 평소 조회에는 카드 정보를 보내지 않습니다.
        self.calls.clear()
        self.handler = self.default_send
        sync.collect(connect, sync.Client(connect), link, "fixture-normal")
        approvals = [body for op, body in self.calls if op == "approval"]
        self.assertEqual(len(approvals), 1)
        self.assertNotIn("cardPassword", approvals[0])
        self.assertEqual(
            self.rows("SELECT state FROM codef_links WHERE card='kb'")[0]["state"], "ready"
        )

        # 하루 첫 조회의 CF-12108에는 저장한 정보로 한 번만 다시 조회합니다.
        def first_of_day(token, payload, operation):
            if operation == "approval" and "cardPassword" not in payload:
                return dict(result={"code": "CF-12108"})
            return self.default_send(token, payload, operation)

        self.calls.clear()
        self.handler = first_of_day
        link = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        sync.collect(connect, sync.Client(connect), link, "fixture-midnight")
        approvals = [body for op, body in self.calls if op == "approval"]
        self.assertEqual(len(approvals), 2)
        self.assertNotIn("cardPassword", approvals[0])
        self.assertEqual(approvals[1]["cardPassword"], sent)
        latest = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertEqual((latest["state"], latest["error_code"]), ("ready", ""))
        exposed = self.partner.get("/api/codef-sync").get_data(as_text=True)
        for secret in ("card_auth_cipher", latest["card_auth_cipher"], sent, "0" * 16):
            self.assertNotIn(secret, exposed)

    def test_kb_saved_possession_failure_stops_without_repeating(self):
        link = self.connect_kb_with_saved_possession()
        self.calls.clear()
        self.handler = lambda token, payload, operation: dict(
            result={"code": "CF-12834" if "cardPassword" in payload else "CF-12108"}
        )
        sync.collect(connect, sync.Client(connect), link, "fixture-wrong-pin")
        self.assertEqual(len(self.calls), 2)
        latest = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertEqual((latest["state"], latest["error_code"]), ("needs_auth", "CF-12834"))
        # 인증 필요 상태의 연결은 다음 정기 회차에 다시 조회하지 않습니다.
        self.calls.clear()
        sync.tick(connect, sync.Client(connect))
        kb = [body for op, body in self.calls if body.get("organization") == "0301"]
        self.assertEqual(kb, [])
        # 같은 회차에 다른 정상 연결은 조회했으므로 작업자가 실제로 돌았습니다.
        self.assertTrue([op for op, _ in self.calls if op == "approval"])

    def test_kb_without_saved_possession_stops_at_cf_12108(self):
        sync.save_link(connect, 2, "kb", "fixture-kb-connected", self.settings, verifying=False)
        link = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.handler = lambda token, payload, operation: dict(result={"code": "CF-12108"})
        sync.collect(connect, sync.Client(connect), link, "fixture-unsaved")
        self.assertEqual(len(self.calls), 1)
        latest = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertEqual((latest["state"], latest["error_code"]), ("needs_auth", "CF-12108"))
        self.assertIn("비밀번호 앞 2자리를", latest["message"])

    def test_kb_possession_from_form_saved_when_kb_does_not_ask_at_setup(self):
        """새 연결 첫 조회에서 KB가 소지 확인을 묻지 않아도 연결 화면 입력으로 확인해 저장합니다.

        2026-11-09 운영에서 이 경우 저장할 기회가 없어 다음 날 00시 조회가 CF-12108로 멈췄습니다.
        """
        start = self.start(self.payload(provider="kb", kb_card_number="0" * 16, kb_pin="00"))
        self.assertEqual(self.wait(start.json["id"])["state"], "success")
        approvals = [body for op, body in self.calls if op == "approval"]
        self.assertEqual(len(approvals), 2)
        self.assertNotIn("cardPassword", approvals[0])
        sent = approvals[1]["cardPassword"]
        self.assertEqual(approvals[1]["cardNo"], "0" * 16)
        link = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        fields, _ = card_auth.load(link, "fixture-new-connected", self.settings, self.key)
        self.assertEqual((fields["cardNo"], fields["cardPassword"]), ("0" * 16, sent))
        self.assertNotIn("0" * 16, str(self.rows("SELECT * FROM connection_setups")))

        # 다음 날 00시처럼 CF-12108을 받으면 저장한 값으로 한 번 다시 조회해 저장합니다.
        def first_of_day(token, payload, operation):
            if operation == "approval" and "cardPassword" not in payload:
                return dict(result={"code": "CF-12108"})
            return self.default_send(token, payload, operation)

        self.calls.clear()
        self.handler = first_of_day
        sync.collect(connect, sync.Client(connect), link, "fixture-midnight")
        latest = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertEqual((latest["state"], latest["error_code"]), ("ready", ""))

    def test_kb_possession_from_form_answers_cf_12108_without_prompt(self):
        def send(token, payload, operation):
            if (
                operation == "approval"
                and len([op for op, _ in self.calls if op == "approval"]) == 1
            ):
                return dict(result={"code": "CF-12108"})
            return self.default_send(token, payload, operation)

        self.handler = send
        start = self.start(self.payload(provider="kb", kb_card_number="0" * 16, kb_pin="00"))
        self.assertEqual(self.wait(start.json["id"])["state"], "success")
        approvals = [body for op, body in self.calls if op == "approval"]
        self.assertEqual(len(approvals), 3)
        self.assertIn("cardPassword", approvals[1])
        self.assertNotIn("cardPassword", approvals[2])
        link = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertTrue(link["card_auth_cipher"])

    def test_kb_possession_from_form_rejected_when_wrong_or_not_kb(self):
        for payload in (
            self.payload(provider="kb", kb_card_number="0" * 16, kb_pin="0000"),
            self.payload(provider="kb", kb_card_number="123", kb_pin="00"),
            self.payload(provider="samsung", kb_card_number="0" * 16, kb_pin="00"),
        ):
            self.assertEqual(self.start(payload).status_code, 400)
        self.assertEqual(self.calls, [])

    def test_kb_possession_not_reflected_waits_40_minutes_then_follow_up_once(self):
        """00시 KB처럼 소지 정보를 보내도 막히면 40분 뒤 한 번만 다시 보고, 그래도 막히면 멈춥니다.

        2026-11-12~14 운영: 소지 정보 조회는 CF-12701, 바로 뒤 조회는 CF-12108이었지만
        40~80분 뒤의 카드 정보 없는 조회는 그대로 성공했습니다.
        """
        link = self.connect_kb_with_saved_possession()

        def midnight(token, payload, operation):
            if operation != "approval":
                return self.default_send(token, payload, operation)
            if "cardPassword" in payload:
                return dict(result={"code": "CF-12701"})
            return dict(result={"code": "CF-12108"})

        self.calls.clear()
        self.handler = midnight
        midnight_time = datetime(2026, 1, 2, 0, 0, 5, tzinfo=sync.KST)
        sync.tick(connect, sync.Client(connect), now=midnight_time)
        kb_calls = [body for _, body in self.calls if body.get("organization") == "0301"]
        self.assertEqual(["cardPassword" in body for body in kb_calls], [False, True])
        latest = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertEqual((latest["state"], latest["error_code"]), ("ready", "CF-12701"))
        self.assertIn("40분 뒤", latest["message"])

        # 40분이 지나기 전에는 다시 조회하지 않습니다.
        self.calls.clear()
        sync.tick(connect, sync.Client(connect), now=midnight_time)
        self.assertEqual([b for _, b in self.calls if b.get("organization") == "0301"], [])

        # 40분 뒤에는 카드 정보 없는 조회가 성공해 저장합니다.
        with connect() as db:
            db.execute(
                "UPDATE codef_links SET last_attempt=last_attempt-? WHERE owner_id=2 AND card='kb'",
                (sync.FOLLOW_UP_DELAY,),
            )
        self.calls.clear()
        self.handler = self.default_send
        sync.tick(connect, sync.Client(connect), now=midnight_time)
        kb_calls = [body for _, body in self.calls if body.get("organization") == "0301"]
        self.assertEqual(["cardPassword" in body for body in kb_calls], [False])
        latest = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertEqual((latest["state"], latest["error_code"]), ("ready", ""))
        # 같은 회차의 재조회는 한 번뿐입니다.
        self.calls.clear()
        sync.tick(connect, sync.Client(connect), now=midnight_time)
        self.assertEqual([b for _, b in self.calls if b.get("organization") == "0301"], [])

    def test_kb_follow_up_still_blocked_stops_after_two_possession_sends(self):
        link = self.connect_kb_with_saved_possession()
        self.calls.clear()
        self.handler = lambda token, payload, operation: dict(result={"code": "CF-12108"})
        sync.collect(connect, sync.Client(connect), link, "2026-01-02T00:00")
        latest = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertEqual((latest["state"], latest["error_code"]), ("ready", "CF-12108"))
        sync.collect(connect, sync.Client(connect), latest, "2026-01-02T00:00" + sync.FOLLOW_UP)
        self.assertEqual(sum("cardPassword" in body for _, body in self.calls), 2)
        latest = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertEqual((latest["state"], latest["error_code"]), ("needs_auth", "CF-12108"))
        self.assertIn("40분 뒤에도", latest["message"])

    def test_card_cf_12701_retries_next_slot_once_then_stops(self):
        """CF-12701은 인증 오류가 아니므로 다음 회차에 한 번 더 조회하고, 연속이면 멈춥니다."""
        sync.save_link(connect, 2, "kb", "fixture-kb-connected", self.settings, verifying=False)
        link = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.handler = lambda token, payload, operation: dict(result={"code": "CF-12701"})
        sync.collect(connect, sync.Client(connect), link, "fixture-first")
        latest = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertEqual((latest["state"], latest["error_code"]), ("ready", "CF-12701"))
        self.assertIn("다음 회차", latest["message"])

        sync.collect(connect, sync.Client(connect), latest, "fixture-second")
        latest = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertEqual((latest["state"], latest["error_code"]), ("error", "CF-12701"))

        # 다음 회차에 성공하면 오류 코드가 지워지고 다시 수집 가능 상태가 됩니다.
        self.handler = self.default_send
        sync.collect(connect, sync.Client(connect), {**latest, "error_code": ""}, "fixture-ok")
        latest = self.rows("SELECT * FROM codef_links WHERE owner_id=2 AND card='kb'")[0]
        self.assertEqual((latest["state"], latest["error_code"]), ("ready", ""))

    def test_login_id_spaces_from_phone_keyboard_are_removed(self):
        start = self.start(self.payload(login_id="  fixture-web-login \t"))
        self.assertEqual(self.wait(start.json["id"])["state"], "success")
        entry = [body for op, body in self.calls if op == "register"][0]["accountList"][0]
        self.assertEqual(entry["id"], "fixture-web-login")
        blank = self.start(self.payload(login_id="   "))
        self.assertEqual(blank.status_code, 400)

    def test_expired_auth_answer_and_interrupted_process_never_retry(self):
        with patch.object(threading.Thread, "start"):
            first = self.start()
        setup.registry.pop(first.json["id"])
        status = self.partner.get("/api/connections/setup/" + first.json["id"])
        self.assertEqual(status.json["state"], "expired")
        self.assertEqual(self.calls, [])

    def test_failure_during_final_store_rolls_back_link_and_transactions(self):
        with patch.object(sync, "store_events", side_effect=RuntimeError("fixture failure")):
            first = self.start()
            self.assertEqual(self.wait(first.json["id"])["state"], "error")
        self.assertEqual(self.rows("SELECT * FROM codef_links WHERE owner_id=2"), [])
        self.assertEqual(self.partner.get("/api/data").json["transactions"], [])
        with patch.object(bank, "store", side_effect=RuntimeError("fixture failure")):
            first = self.start(self.payload("bank", "woori"))
            self.assertEqual(self.wait(first.json["id"])["state"], "error")
        self.assertEqual(self.rows("SELECT * FROM bank_links WHERE owner_id=2"), [])
        self.assertEqual(self.rows("SELECT * FROM bank_transactions"), [])

    def test_expired_challenge_cannot_continue_or_store(self):
        self.handler = lambda *args: dict(
            result={"code": "CF-03002"},
            data=dict(
                continue2Way=True,
                method="smsAuthNo",
                jobIndex=1,
                threadIndex=1,
                jti="fixture",
                twoWayTimestamp=1,
            ),
        )
        first = self.start()
        challenge = self.wait(first.json["id"], states=("needs_input",))
        job = setup.registry[first.json["id"]]
        with job.condition:
            job.deadline = time.time() - 1
            job.condition.notify_all()
        reply = self.partner.post(
            "/api/connections/setup/" + first.json["id"] + "/answer",
            json={"challenge_id": challenge["challenge_id"], "answer": "123456"},
            headers=HEADERS,
        )
        self.assertEqual(reply.status_code, 409)
        self.assertEqual(self.wait(first.json["id"])["state"], "error")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.rows("SELECT * FROM codef_links WHERE owner_id=2"), [])

    def test_key_change_while_in_flight_discards_reply(self):
        entered, release = self.gate_query()
        first = self.start()
        self.assertTrue(entered.wait(3))
        Path(self.root.name, "key").write_bytes(Fernet.generate_key())
        release.set()
        self.assertEqual(self.wait(first.json["id"])["state"], "error")
        self.assertEqual(self.rows("SELECT * FROM codef_links WHERE owner_id=2"), [])

    def test_cancelled_bank_query_preserves_existing_bank_rows(self):
        entered, release = self.gate_query()
        first = self.start(self.payload("bank", "woori"))
        self.assertTrue(entered.wait(3))
        self.partner.post("/api/connections/setup/" + first.json["id"] + "/cancel", headers=HEADERS)
        release.set()
        setup.registry[first.json["id"]].thread.join(3)
        self.assertEqual(self.rows("SELECT * FROM bank_links"), [])
        self.assertEqual(self.rows("SELECT * FROM bank_transactions"), [])

    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_phone_connection_and_additional_auth_without_browser_storage(self):
        from werkzeug.serving import make_server, WSGIRequestHandler

        class Quiet(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        def send(token, payload, operation):
            if operation == "register" and not payload.get("is2Way"):
                return dict(
                    result={"code": "CF-03002"},
                    data=dict(
                        continue2Way=True,
                        method="smsAuthNo",
                        jobIndex=1,
                        threadIndex=1,
                        jti="fixture-session",
                        twoWayTimestamp=1,
                    ),
                )
            return self.default_send(token, payload, operation)

        self.handler = send
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                ["node", "tests/connection-setup-ui.cjs"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
                check=True,
                timeout=150,
            )
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
