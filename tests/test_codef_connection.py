"""가상 RSA 키·토큰·응답으로 CODEF 설정 보호와 소유자 인증 확인을 검증합니다.

실제 CODEF 서버·키·카드 데이터는 사용하지 않습니다. 네트워크는 전부 대체하고 토큰이
응답·DB·이력에 남지 않는지, 네트워크 중 DB 잠금이 풀리는지 확인합니다.
"""

import base64
import errno
import importlib.util
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import app, connect
from auth import set_password
import codef_connection as codef

HEADERS = {"X-Requested-With": "CardDashboard"}
PUBLIC_KEY = rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key()
PUBLIC_B64 = base64.b64encode(
    PUBLIC_KEY.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
).decode()
SETTINGS = dict(
    environment="demo",
    client_id="fixture-codef-id",
    client_secret="fixture-codef-secret",
    public_key=PUBLIC_B64,
)


class Response(io.BytesIO):
    status = 200


class CodefTransportTests(unittest.TestCase):
    def test_https_fixed_host_basic_auth_and_token_is_discarded(self):
        with patch.object(codef.urllib.request, "build_opener") as builder:
            builder.return_value.open.return_value = Response(
                json.dumps(dict(access_token="fixture-access-token", token_type="Bearer")).encode()
            )
            self.assertIsNone(codef.check_developer_token(SETTINGS))
            args, kwargs = builder.return_value.open.call_args
            outgoing = args[0]
            self.assertEqual(outgoing.full_url, codef.TOKEN_URL)
            self.assertEqual(outgoing.get_method(), "POST")
            self.assertEqual(
                outgoing.get_header("Authorization"),
                "Basic " + base64.b64encode(b"fixture-codef-id:fixture-codef-secret").decode(),
            )
            self.assertEqual(outgoing.data, b"grant_type=client_credentials&scope=read")
            self.assertEqual(kwargs["timeout"], 10)
        self.assertIsNone(
            codef.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")
        )

    def test_provider_errors_bad_json_and_unbounded_response_are_not_echoed(self):
        cases = [
            urllib.error.HTTPError(
                codef.TOKEN_URL, 401, "PRIVATE_PROVIDER_MESSAGE", {}, io.BytesIO(b"PRIVATE_BODY")
            ),
            urllib.error.URLError("PRIVATE_TLS_MESSAGE"),
            http.client.IncompleteRead(b"PRIVATE_BODY", 1),
            Response(b'{"error":"PRIVATE_BODY"}'),
            Response(b"PRIVATE_NON_JSON"),
            Response(b"x" * (codef.RESPONSE_LIMIT + 1)),
            Response(b'{"access_token":"a b"}'),
            Response(b'{"access_token":"token","token_type":"Other"}'),
        ]
        for item in cases:
            with patch.object(codef.urllib.request, "build_opener") as builder:
                if isinstance(item, Exception):
                    builder.return_value.open.side_effect = item
                else:
                    builder.return_value.open.return_value = item
                with self.assertRaises(codef.ConnectionCheckError) as caught:
                    codef.check_developer_token(SETTINGS)
                self.assertNotIn("PRIVATE_", str(caught.exception))

    def test_settings_validate_rsa_environment_and_credential_headers(self):
        self.assertEqual(codef.validate_settings(SETTINGS), SETTINGS)
        pem = PUBLIC_KEY.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        self.assertEqual(
            codef.validate_settings({**SETTINGS, "public_key": pem})["public_key"], pem.strip()
        )
        for bad in [
            [],
            {**SETTINGS, "environment": "production"},
            {**SETTINGS, "public_key": "bad"},
            {**SETTINGS, "client_id": "a:b"},
            {**SETTINGS, "client_secret": "value\nheader"},
        ]:
            with self.assertRaises(ValueError):
                codef.validate_settings(bad)

    def test_provider_public_key_size_and_pem_variants(self):
        # 전송용 키의 생성 정책이 아니라, 공급자가 준 공개키를 읽는 호환성 검증입니다.
        number = (PUBLIC_KEY.public_numbers().n >> 512) | 1
        short_key = rsa.RSAPublicNumbers(65537, number).public_key()
        self.assertEqual(short_key.key_size, 512)
        for key in (short_key, PUBLIC_KEY):
            der = base64.b64encode(
                key.public_bytes(
                    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
                )
            ).decode()
            pem = key.public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.PKCS1
            ).decode()
            for value in (der, pem):
                self.assertEqual(codef.validate_public_key(value), value.strip())
        for field in ("client_id", "client_secret"):
            with self.assertRaises(ValueError) as caught:
                codef.validate_credential(field, "PRIVATE_VALUE WITH SPACE")
            self.assertIn(field, str(caught.exception))
            self.assertNotIn("PRIVATE", str(caught.exception))


class CodefApiTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.directory.name) / "db.sqlite3")
        )
        self.path = Path(self.directory.name) / "codef-app.json"
        self.environment = patch.dict(os.environ, {"MOA_CODEF_CONFIG_FILE": str(self.path)})
        self.environment.start()
        set_password(connect, "fixture-owner-password")
        self.owner = app.test_client()
        self.owner.post("/api/login", json={"password": "fixture-owner-password"}, headers=HEADERS)
        revision = self.owner.get("/api/members").headers["X-Moa-Revision"]
        self.owner.post(
            "/api/members",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers={**HEADERS, "If-Match": revision},
        )
        self.partner = app.test_client()
        self.partner.post(
            "/api/login",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers=HEADERS,
        )

    def tearDown(self):
        self.environment.stop()
        app.config.update(self.previous)
        self.directory.cleanup()

    def configure(self):
        self.path.write_text(json.dumps(SETTINGS))

    def test_absent_config_auth_owner_only_and_no_automatic_calls(self):
        with patch.object(codef, "check_developer_token") as network:
            self.assertFalse(self.owner.get("/api/codef/status").json["configured"])
            self.configure()
            self.assertTrue(self.owner.get("/api/codef/status").json["configured"])
            self.assertIsNone(self.owner.get("/api/codef/status").json["ok"])
            network.assert_not_called()
            self.assertEqual(app.test_client().get("/api/codef/status").status_code, 401)
            self.assertEqual(self.partner.get("/api/codef/status").status_code, 403)
            self.assertEqual(
                self.partner.post("/api/codef/check", headers=HEADERS).status_code, 403
            )
            self.assertEqual(self.owner.post("/api/codef/check").status_code, 403)
            self.assertEqual(
                self.owner.post(
                    "/api/codef/check", headers={**HEADERS, "Origin": "https://other.invalid"}
                ).status_code,
                403,
            )
            network.assert_not_called()

    def test_success_status_is_redacted_persistent_and_rate_limited(self):
        self.configure()
        before = self.owner.get("/api/data").headers["X-Moa-Revision"]
        with patch.object(codef, "check_developer_token") as network:
            response = self.owner.post("/api/codef/check", headers=HEADERS)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["ok"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertTrue(self.owner.get("/api/codef/status").json["ok"])
            limited = self.owner.post("/api/codef/check", headers=HEADERS)
            self.assertEqual(limited.status_code, 429)
            self.assertIn("Retry-After", limited.headers)
            self.assertEqual(network.call_count, 1)
        db = connect()
        try:
            dump = "\n".join(db.iterdump())
        finally:
            db.close()
        for value in (
            SETTINGS["client_id"],
            SETTINGS["client_secret"],
            PUBLIC_B64,
            "fixture-access-token",
        ):
            self.assertNotIn(value, response.text)
            self.assertNotIn(value, dump)
        # 인증 확인은 소비 자료 변경이 아니며 개인 원장 버전을 올리지 않습니다.
        self.assertEqual(self.owner.get("/api/data").headers["X-Moa-Revision"], before)
        self.assertEqual(self.owner.get("/api/data").json["transactions"], [])
        self.assertEqual(
            self.owner.get("/api/audit").json["items"][0]["action"], "CODEF 개발자 인증 확인"
        )

    def test_network_does_not_hold_database_lock_and_revoked_session_cannot_finish(self):
        self.configure()

        def network(settings):
            db = sqlite3.connect(app.config["DATABASE"], timeout=0.1)
            with db:
                db.execute("DELETE FROM sessions WHERE user_id=1")
            db.close()

        with patch.object(codef, "check_developer_token", side_effect=network):
            self.assertEqual(self.owner.post("/api/codef/check", headers=HEADERS).status_code, 401)
        db = connect()
        try:
            self.assertIsNone(db.execute("SELECT ok FROM codef_app_check").fetchone()[0])
        finally:
            db.close()

    def test_safe_failure_config_rotation_and_invalid_file(self):
        self.configure()
        with patch.object(
            codef,
            "check_developer_token",
            side_effect=codef.ConnectionCheckError("인증 서버 연결 실패"),
        ):
            result = self.owner.post("/api/codef/check", headers=HEADERS)
            self.assertFalse(result.json["ok"])
        changed = {**SETTINGS, "client_secret": "fixture-new-secret"}
        self.path.write_text(json.dumps(changed))
        self.assertIsNone(self.owner.get("/api/codef/status").json["ok"])
        with patch.object(codef, "check_developer_token"):
            self.assertTrue(self.owner.post("/api/codef/check", headers=HEADERS).json["ok"])
        self.path.write_text("PRIVATE_BAD_JSON")
        response = self.owner.get("/api/codef/status")
        self.assertFalse(response.json["configured"])
        self.assertNotIn("PRIVATE_", response.text)
        with patch.object(codef, "check_developer_token") as network:
            self.assertEqual(self.owner.post("/api/codef/check", headers=HEADERS).status_code, 400)
            network.assert_not_called()


class CodefSetupTests(unittest.TestCase):
    def test_frontend_request_flow_without_browser(self):
        import subprocess

        subprocess.run(
            ["node", "tests/codef-ui-logic.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            timeout=10,
        )

    def setup_module(self):
        spec = importlib.util.spec_from_file_location(
            "configure_codef_test",
            Path(__file__).resolve().parents[1] / "scripts/configure-codef.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_prompt_retries_only_invalid_field_and_does_not_echo_input(self):
        module = self.setup_module()
        captured = {}
        with (
            patch.object(module.sys, "argv", ["configure-codef.py"]),
            patch.object(module.sys.stdin, "isatty", return_value=True),
            patch.object(
                module.getpass,
                "getpass",
                side_effect=[
                    "",
                    SETTINGS["client_id"],
                    SETTINGS["client_secret"],
                    "PRIVATE_BAD_PUBLIC",
                    PUBLIC_B64,
                ],
            ) as prompts,
            patch.object(
                module,
                "save_configuration",
                side_effect=lambda root, settings: captured.update(settings),
            ),
            patch("builtins.print") as output,
        ):
            module.main()
        self.assertEqual(prompts.call_count, 5)
        self.assertEqual(captured, SETTINGS)
        messages = " ".join(str(item) for call in output.call_args_list for item in call.args)
        for value in (
            SETTINGS["client_id"],
            SETTINGS["client_secret"],
            PUBLIC_B64,
            "PRIVATE_BAD_PUBLIC",
        ):
            self.assertNotIn(value, messages)
        self.assertIn("client_id 형식: 정상", messages)
        self.assertIn("public_key 형식: 정상", messages)

    def test_permission_and_readonly_errors_are_safe_and_preserve_old_file(self):
        module = self.setup_module()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(module.os, "chown"),
            patch.object(module.os, "fchown"),
        ):
            root = Path(directory) / "secrets"
            module.save_configuration(root, SETTINGS)
            old = (root / "codef-app.json").read_bytes()
            with patch.object(
                module.os, "chown", side_effect=PermissionError(errno.EPERM, "PRIVATE_PATH")
            ):
                with self.assertRaises(module.SettingsWriteError) as caught:
                    module.save_configuration(root, SETTINGS)
            self.assertIn("폴더 권한", str(caught.exception))
            self.assertIn("--user 0", str(caught.exception))
            self.assertNotIn("PRIVATE", str(caught.exception))
            with patch.object(
                module.Path, "replace", side_effect=OSError(errno.EROFS, "PRIVATE_PATH")
            ):
                with self.assertRaises(module.SettingsWriteError) as caught:
                    module.save_configuration(root, SETTINGS)
            self.assertIn("기존 설정 교체", str(caught.exception))
            self.assertIn("읽기 전용", str(caught.exception))
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertEqual((root / "codef-app.json").read_bytes(), old)
            self.assertEqual(len(list(root.iterdir())), 1)
        self.assertIn(
            "저장 공간",
            str(module.safe_write_error("파일 저장", OSError(errno.ENOSPC, "PRIVATE_PATH"))),
        )

    def test_diagnose_does_not_read_existing_keys(self):
        module = self.setup_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "codef-app.json").write_text("PRIVATE_CONFIGURATION")
            with (
                patch.object(
                    module.Path, "read_text", side_effect=AssertionError("must not read keys")
                ),
                patch("builtins.print") as output,
            ):
                module.diagnose(root)
            messages = " ".join(str(item) for call in output.call_args_list for item in call.args)
            self.assertNotIn("PRIVATE", messages)
            self.assertIn("임시 파일 쓰기: 정상", messages)

    def test_atomic_private_config_and_invalid_input_preserves_previous_file(self):
        spec = importlib.util.spec_from_file_location(
            "configure_codef", Path(__file__).resolve().parents[1] / "scripts/configure-codef.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(module.os, "chown"),
            patch.object(module.os, "fchown"),
        ):
            root = Path(directory) / "secrets"
            module.save_configuration(root, SETTINGS)
            result = root / "codef-app.json"
            self.assertEqual(result.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(result.read_text()), SETTINGS)
            with self.assertRaises(ValueError):
                module.save_configuration(root, {**SETTINGS, "public_key": "bad"})
            self.assertEqual(json.loads(result.read_text()), SETTINGS)
            self.assertEqual(len(list(root.iterdir())), 1)


class CodefBrowserTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_mobile_developer_check_and_no_secret_inputs(self):
        import subprocess
        import threading
        from werkzeug.serving import make_server, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        previous = dict(app.config)
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "codef-app.json"
            settings_path.write_text(json.dumps(SETTINGS))
            app.config.update(
                TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(directory) / "db.sqlite3")
            )
            set_password(connect, "fixture-owner-password")
            server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            with (
                patch.dict(os.environ, {"MOA_CODEF_CONFIG_FILE": str(settings_path)}),
                patch.object(codef, "check_developer_token"),
            ):
                thread.start()
                try:
                    subprocess.run(
                        ["node", "tests/codef-ui.cjs"],
                        cwd=Path(__file__).resolve().parents[1],
                        env={
                            **os.environ,
                            "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}",
                        },
                        check=True,
                        timeout=40,
                    )
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()
                    app.config.update(previous)
