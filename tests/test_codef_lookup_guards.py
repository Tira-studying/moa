"""가상 승인 응답으로 자동 조회 도중 키·설정 변경을 저장 직전까지 차단하는지 확인합니다.

실제 CODEF 연결 없이 임시 DB·키를 사용합니다. 중단된 조회가 기존 지출이나 마지막
성공 시각을 바꾸거나, 설정 오류를 잘못된 비밀번호 상태로 표시하지 않는지 확인합니다.
"""

from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from app import app, connect
from auth import set_password
import codef_sync as sync


SETTINGS = dict(
    environment="demo",
    client_id="fixture-id",
    client_secret="fixture-secret",
    public_key="fixture-public",
)


def fixture_row(approval="fixture-approval"):
    """승인번호·금액·가맹점은 이 검증에서만 사용하는 가상 자료입니다."""
    return dict(
        resUsedDate=datetime.now(sync.KST).strftime("%Y%m%d"),
        resUsedTime="120000",
        resUsedAmount="2400",
        resCancelYN="0",
        resAccountCurrency="KRW",
        resMemberStoreName="가상 서점",
        resCardNo="****0000",
        resApprovalNo=approval,
    )


class CodefLookupGuardTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        app.config.update(TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(root / "db.sqlite3"))
        self.key = root / "fixture-key"
        self.key.write_bytes(Fernet.generate_key())
        self.environment = patch.dict(os.environ, {"MOA_CODEF_STORAGE_KEY_FILE": str(self.key)})
        self.environment.start()
        self.configuration = patch.object(sync, "configuration", return_value=SETTINGS)
        self.settings_reader = self.configuration.start()
        set_password(connect, "fixture-owner-password")
        sync.save_link(connect, 1, "samsung", "fixture-connected", SETTINGS, verifying=False)

    def tearDown(self):
        self.configuration.stop()
        self.environment.stop()
        app.config.update(self.previous)
        self.directory.cleanup()

    def fetch(self, sql, parameters=()):
        db = connect()
        try:
            return db.execute(sql, parameters).fetchall()
        finally:
            db.close()

    def link(self):
        return dict(self.fetch("SELECT * FROM codef_links")[0])

    def client(self, approval="fixture-approval"):
        client = Mock()
        client.credentials.return_value = (SETTINGS, "fixture-token")
        client.send.return_value = {
            "result": {"code": "CF-00000"},
            "data": [fixture_row(approval)],
        }
        return client

    def prepare_success(self):
        """이전 성공 자료를 먼저 남겨 새 조회 실패가 기존 결과를 덮지 않는지 구분합니다."""
        sync.collect(connect, self.client(), self.link(), "fixture-previous-success")
        db = connect()
        try:
            with db:
                db.execute("UPDATE codef_links SET last_success=42")
        finally:
            db.close()

    def change(self, kind):
        if kind == "storage":
            self.key.write_bytes(Fernet.generate_key())
        elif kind == "settings":
            self.settings_reader.return_value = {**SETTINGS, "client_id": "fixture-changed-id"}
        elif kind == "missing":
            self.key.unlink()
        else:
            self.settings_reader.side_effect = ValueError("fixture-private-error")

    def assert_interrupted_result(self):
        link = self.link()
        self.assertEqual(link["state"], "error")
        self.assertEqual(link["error_code"], "")
        self.assertEqual(link["last_success"], 42)
        self.assertEqual(link["message"], "처리 중 CODEF 키 설정이 바뀌어 저장하지 않았습니다.")
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 1)
        self.assertEqual(len(self.fetch("SELECT * FROM codef_events")), 1)
        self.assertEqual(self.fetch("SELECT state FROM codef_jobs ORDER BY id")[-1][0], "error")

    def assert_change_discarded(self, kind, phase):
        self.prepare_success()
        client = self.client("fixture-next-approval")
        if phase == "response":
            response = client.send.return_value

            def send(*args):
                self.change(kind)
                return response

            client.send.side_effect = send
            sync.collect(connect, client, self.link(), "fixture-interrupted")
        else:
            normalize = sync.normalize

            def normalize_then_change(*args):
                items = normalize(*args)
                self.change(kind)
                return items

            with patch.object(sync, "normalize", side_effect=normalize_then_change):
                sync.collect(connect, client, self.link(), "fixture-interrupted")
        client.send.assert_called_once()
        self.assert_interrupted_result()

    def test_storage_key_rotation_during_response_discards_new_records(self):
        self.assert_change_discarded("storage", "response")

    def test_app_key_rotation_during_response_discards_new_records(self):
        self.assert_change_discarded("settings", "response")

    def test_storage_key_rotation_after_normalization_discards_new_records(self):
        self.assert_change_discarded("storage", "normalization")

    def test_app_key_rotation_after_normalization_discards_new_records(self):
        self.assert_change_discarded("settings", "normalization")

    def test_storage_key_removed_after_normalization_keeps_safe_error_and_previous_success(self):
        self.assert_change_discarded("missing", "normalization")

    def test_unreadable_app_configuration_after_normalization_does_not_expose_exception(self):
        self.assert_change_discarded("invalid", "normalization")

    def test_unchanged_settings_keep_lookup_idempotent_and_slot_deduplication(self):
        client = self.client()
        for slot in ("fixture-first", "fixture-first", "fixture-second"):
            sync.collect(connect, client, self.link(), slot)
        self.assertEqual(client.send.call_count, 2)
        self.assertEqual(self.link()["state"], "ready")
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 1)
        self.assertEqual(len(self.fetch("SELECT * FROM codef_events")), 1)
        self.assertEqual(len(self.fetch("SELECT * FROM codef_jobs")), 2)


if __name__ == "__main__":
    unittest.main()
