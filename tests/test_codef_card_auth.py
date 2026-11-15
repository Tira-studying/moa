"""가상 인증정보로 현대 암호문 재사용·계정 결합·원자적 활성화·비밀 미노출을 검증합니다.

실제 카드번호·PIN·CODEF 응답은 사용하지 않으며 모든 DB·비밀 파일은 임시 폴더에 둡니다.
"""

import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from app import app, connect
from auth import set_password
import codef_card_auth as auth
import codef_sync as sync
from codef_card import CardCheckError
from test_codef_sync import SETTINGS, approval

NUMBER = "0" * 16
PIN_CIPHER = base64.b64encode(b"fixture-rsa-cipher" * 8).decode()


class CardAuthTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.secret = self.root / "card-auth"
        self.key_path = self.root / "key"
        self.key_path.write_bytes(Fernet.generate_key())
        app.config.update(TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(self.root / "db.sqlite3"))
        self.environment = patch.dict(
            os.environ,
            {
                "MOA_CODEF_STORAGE_KEY_FILE": str(self.key_path),
                "MOA_CODEF_CARD_AUTH_DIR": str(self.secret),
                "MOA_CODEF_CARD_AUTH_SETUP_DIR": str(self.secret),
            },
        )
        self.environment.start()
        self.config = patch.object(sync, "configuration", return_value=SETTINGS)
        self.config.start()
        set_password(connect, "fixture-owner-password")
        sync.save_link(connect, 1, "hyundai", "fixture-connected", SETTINGS)
        spec = importlib.util.spec_from_file_location(
            "configure_card_auth",
            Path(__file__).resolve().parents[1] / "scripts/configure-codef-card-auth.py",
        )
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def tearDown(self):
        self.config.stop()
        self.environment.stop()
        app.config.update(self.previous)
        self.directory.cleanup()

    def link(self):
        db = connect()
        try:
            return dict(
                db.execute(
                    "SELECT * FROM codef_links WHERE owner_id=1 AND card='hyundai'"
                ).fetchone()
            )
        finally:
            db.close()

    def query(self, sql):
        db = connect()
        try:
            return db.execute(sql).fetchall()
        finally:
            db.close()

    def store(self):
        link = self.link()
        ref = auth.save(link, "fixture-connected", SETTINGS, sync.storage_key(), NUMBER, PIN_CIPHER)
        db = connect()
        try:
            with db:
                db.execute("UPDATE codef_links SET card_auth_ref=? WHERE owner_id=1", (ref,))
        finally:
            db.close()
        return self.link(), self.secret / ref

    def client(self, rows=None):
        client = Mock()
        client.credentials.return_value = (SETTINGS, "fixture-token")
        client.send.return_value = {"result": {"code": "CF-00000"}, "data": rows or []}
        return client

    def test_same_rsa_cipher_is_reused_twice_and_not_saved_during_probe(self):
        client = self.client()
        # 실제 전송처럼 요청을 복사해 두어 마지막 메모리 정리와 구분합니다.
        requests = []
        client.send.side_effect = lambda token, body: requests.append(dict(body)) or {
            "result": {"code": "CF-00000"},
            "data": [],
        }
        guard = Mock()
        auth.verify_reuse(
            client, "fixture-token", NUMBER, PIN_CIPHER, "fixture-connected", "20260101", guard
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0]["cardPassword"], PIN_CIPHER)
        self.assertEqual(requests[0]["inquiryType"], "1")
        self.assertEqual(guard.call_count, 2)
        self.assertFalse(self.secret.exists())

    def test_failed_first_probe_does_not_retry_or_save(self):
        client = self.client()
        client.send.return_value = {"result": {"code": "CF-12834", "message": NUMBER}}
        with self.assertRaises(CardCheckError) as error:
            auth.verify_reuse(
                client, "fixture-token", NUMBER, PIN_CIPHER, "fixture-connected", "20260101", Mock()
            )
        self.assertEqual(client.send.call_count, 1)
        self.assertNotIn(NUMBER, str(error.exception))
        self.assertFalse(self.secret.exists())

    def test_storage_is_encrypted_restricted_and_bound_to_owner_connection_and_app(self):
        link, path = self.store()
        blob = path.read_bytes()
        self.assertNotIn(NUMBER.encode(), blob)
        self.assertNotIn(PIN_CIPHER.encode(), blob)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o700)
        fields, snapshot = auth.load(link, "fixture-connected", SETTINGS, sync.storage_key())
        self.assertEqual(fields["cardPassword"], PIN_CIPHER)
        self.assertEqual(fields["cardNo"], NUMBER)
        self.assertTrue(snapshot)
        for modified, connected, settings in (
            ({**link, "owner_id": 2}, "fixture-connected", SETTINGS),
            (link, "fixture-other-connected", SETTINGS),
            (link, "fixture-connected", {**SETTINGS, "client_id": "other-fixture"}),
        ):
            with self.assertRaises(auth.CardAuthError) as error:
                auth.load(modified, connected, settings, sync.storage_key())
            self.assertNotIn(NUMBER, str(error.exception))
        self.assertNotIn(NUMBER.encode(), Path(app.config["DATABASE"]).read_bytes())
        self.assertNotIn(PIN_CIPHER.encode(), Path(app.config["DATABASE"]).read_bytes())

    def test_plain_pin_and_unsafe_paths_are_rejected(self):
        for value in ("0000", "fixture-plain-pin", "", "0" * 5000):
            with self.assertRaises(auth.CardAuthError):
                auth.save(
                    self.link(), "fixture-connected", SETTINGS, sync.storage_key(), NUMBER, value
                )
        with self.assertRaises(auth.CardAuthError):
            auth.load(
                {**self.link(), "card_auth_ref": "../../fixture"},
                "fixture-connected",
                SETTINGS,
                sync.storage_key(),
            )
        self.assertFalse(self.secret.exists())

    def test_symlink_and_public_file_permissions_are_rejected(self):
        link, path = self.store()
        path.chmod(0o644)
        with self.assertRaises(auth.CardAuthError):
            auth.load(link, "fixture-connected", SETTINGS, sync.storage_key())
        path.chmod(0o600)
        original = path.with_suffix(".original")
        path.rename(original)
        path.symlink_to(original)
        with self.assertRaises(auth.CardAuthError):
            auth.load(link, "fixture-connected", SETTINGS, sync.storage_key())

    def test_worker_uses_saved_cipher_without_credentials_and_requery_does_not_duplicate(self):
        link, path = self.store()
        client = self.client([approval()])
        requests = []
        client.send.side_effect = lambda token, body: requests.append(dict(body)) or {
            "result": {"code": "CF-00000"},
            "data": [approval()],
        }
        sync.collect(connect, client, link, "fixture-first")
        sync.collect(connect, client, self.link(), "fixture-second")
        self.assertEqual(self.link()["state"], "ready")
        self.assertEqual(len(self.query("SELECT * FROM transactions")), 1)
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request["cardPassword"], PIN_CIPHER)
            self.assertNotIn("id", request)
            self.assertNotIn("password", request)

    def test_corrupt_or_replaced_file_does_not_reach_network_or_change_ledger(self):
        link, path = self.store()
        path.write_bytes(b"fixture-corrupt")
        client = self.client([approval()])
        sync.collect(connect, client, link, "fixture-corrupt")
        client.send.assert_not_called()
        self.assertEqual(self.link()["state"], "needs_auth")
        self.assertIsNone(self.link()["last_success"])
        self.assertEqual(len(self.query("SELECT * FROM transactions")), 0)

    def test_file_changed_during_network_discards_late_result(self):
        link, path = self.store()
        client = self.client()

        def changed(token, body):
            path.write_bytes(b"fixture-replaced")
            return {"result": {"code": "CF-00000"}, "data": [approval()]}

        client.send.side_effect = changed
        sync.collect(connect, client, link, "fixture-file-change")
        self.assertNotEqual(self.link()["state"], "ready")
        self.assertIsNone(self.link()["last_success"])
        self.assertEqual(len(self.query("SELECT * FROM transactions")), 0)

    def test_activation_requires_two_probes_and_a_fresh_secret_file_read(self):
        client = self.client([approval()])
        requests = []
        client.send.side_effect = lambda token, body: requests.append(dict(body)) or {
            "result": {"code": "CF-00000"},
            "data": [approval()],
        }
        with patch("builtins.print") as output:
            result = self.cli.configure(connect, client, self.link(), SETTINGS, NUMBER, PIN_CIPHER)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0]["cardPassword"], requests[1]["cardPassword"])
        self.assertEqual(requests[1]["cardPassword"], requests[2]["cardPassword"])
        self.assertTrue(self.link()["card_auth_ref"])
        self.assertEqual(len(self.query("SELECT * FROM transactions")), 1)
        self.assertNotIn(NUMBER, str(output.call_args_list))
        self.assertNotIn(PIN_CIPHER, str(output.call_args_list))

    def test_failed_second_probe_preserves_existing_setting(self):
        link, path = self.store()
        original = path.read_bytes()
        client = self.client()
        client.send.side_effect = [
            {"result": {"code": "CF-00000"}, "data": []},
            {"result": {"code": "CF-12834"}},
        ]
        with patch("builtins.print"), self.assertRaises(CardCheckError):
            self.cli.configure(connect, client, link, SETTINGS, NUMBER, PIN_CIPHER)
        self.assertEqual(client.send.call_count, 2)
        self.assertEqual(self.link(), link)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(list(self.secret.glob("*.enc"))), 1)

    def test_connection_paused_after_first_probe_stops_before_second(self):
        client = self.client()

        def paused(token, body):
            db = connect()
            try:
                with db:
                    db.execute(
                        "UPDATE codef_links SET enabled=0,version=version+1 WHERE owner_id=1"
                    )
            finally:
                db.close()
            return {"result": {"code": "CF-00000"}, "data": []}

        client.send.side_effect = paused
        with patch("builtins.print"), self.assertRaises(sync.SyncError):
            self.cli.configure(connect, client, self.link(), SETTINGS, NUMBER, PIN_CIPHER)
        self.assertEqual(client.send.call_count, 1)
        self.assertFalse(self.secret.exists())

    def test_read_mount_mismatch_cleans_unreferenced_file(self):
        link = self.link()
        with patch.object(
            self.cli.card_auth, "load", side_effect=auth.CardAuthError("가상 읽기 실패")
        ):
            with patch("builtins.print"), self.assertRaises(auth.CardAuthError):
                self.cli.configure(connect, self.client(), link, SETTINGS, NUMBER, PIN_CIPHER)
        self.assertEqual(self.link(), link)
        self.assertEqual(list(self.secret.glob("*.enc")), [])

    def test_pause_after_secret_creation_cleans_new_file_without_reenabling(self):
        original_save = auth.save

        def save_then_pause(*args):
            ref = original_save(*args)
            db = connect()
            try:
                with db:
                    db.execute(
                        "UPDATE codef_links SET enabled=0,version=version+1 WHERE owner_id=1"
                    )
            finally:
                db.close()
            return ref

        with patch.object(auth, "save", side_effect=save_then_pause):
            with patch("builtins.print"), self.assertRaises(sync.SyncError):
                self.cli.configure(
                    connect, self.client(), self.link(), SETTINGS, NUMBER, PIN_CIPHER
                )
        self.assertEqual(self.link()["card_auth_ref"], "")
        self.assertFalse(self.link()["enabled"])
        self.assertEqual(list(self.secret.glob("*.enc")), [])

    def test_cli_rejects_secret_arguments_and_does_not_print_hidden_inputs(self):
        with patch.object(
            self.cli.sys, "argv", ["configure", "--user", "owner", "fixture-private-argument"]
        ):
            with patch("builtins.print") as output:
                self.assertEqual(self.cli.main(), 1)
        self.assertNotIn("fixture-private-argument", str(output.call_args_list))
        operator = {"read_hidden": Mock(side_effect=[NUMBER, "1234"])}
        with (
            patch.object(self.cli.sys, "argv", ["configure", "--user", "owner"]),
            patch.object(self.cli.sys.stdin, "isatty", return_value=True),
            patch.object(self.cli.sys.stdout, "isatty", return_value=True),
            patch("codef_connection.configuration", return_value=SETTINGS),
            patch.object(self.cli, "configuration", return_value=SETTINGS),
            patch("runpy.run_path", return_value=operator),
            patch.object(self.cli, "encrypt_password", return_value=PIN_CIPHER) as encrypt,
            patch.object(
                self.cli, "configure", return_value={"state": "ready", "message": "가상 성공"}
            ) as configure,
            patch("builtins.print") as output,
        ):
            self.assertEqual(self.cli.main(), 0)
        encrypt.assert_called_once_with(SETTINGS["public_key"], "1234")
        self.assertEqual(configure.call_args.args[-1], PIN_CIPHER)
        for secret in (NUMBER, "1234", PIN_CIPHER):
            self.assertNotIn(secret, str(output.call_args_list))

    def test_later_connection_change_is_not_adopted_by_activation_command(self):
        link, path = self.store()
        original_remove = auth.remove

        def remove_then_change(*args):
            original_remove(*args)
            db = connect()
            try:
                with db:
                    db.execute("UPDATE codef_links SET version=version+1 WHERE owner_id=1")
            finally:
                db.close()

        with (
            patch.object(auth, "remove", side_effect=remove_then_change),
            patch.object(self.cli, "collect") as collect,
            patch("builtins.print"),
            self.assertRaises(sync.SyncError),
        ):
            self.cli.configure(connect, self.client(), link, SETTINGS, NUMBER, PIN_CIPHER)
        collect.assert_not_called()

    def test_disabling_keeps_id_ledger_and_other_cards(self):
        link, path = self.store()
        client = self.client([approval()])
        sync.collect(connect, client, link, "fixture-before-disable")
        before = self.link()["cipher"]
        self.cli.disable(connect, self.link())
        self.assertEqual(self.link()["cipher"], before)
        self.assertEqual(self.link()["card_auth_ref"], "")
        self.assertEqual(self.link()["state"], "needs_auth")
        self.assertFalse(path.exists())
        self.assertEqual(len(self.query("SELECT * FROM transactions")), 1)

    def test_api_does_not_return_file_reference_or_secrets(self):
        self.store()
        client = app.test_client()
        client.post(
            "/api/login",
            json={"username": "owner", "password": "fixture-owner-password"},
            headers={"X-Requested-With": "CardDashboard"},
        )
        response = client.get("/api/codef-sync")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        for value in (NUMBER, PIN_CIPHER, self.link()["card_auth_ref"], "card_auth_ref"):
            self.assertNotIn(value, text)

    def test_schema_upgrade_preserves_existing_connection(self):
        before = self.link()["cipher"]
        db = connect()
        try:
            db.execute("ALTER TABLE codef_links DROP COLUMN card_auth_ref")
            sync.schema(db)
            self.assertEqual(
                db.execute("SELECT cipher FROM codef_links WHERE owner_id=1").fetchone()[0], before
            )
            self.assertEqual(
                db.execute("SELECT card_auth_ref FROM codef_links WHERE owner_id=1").fetchone()[0],
                "",
            )
        finally:
            db.close()
