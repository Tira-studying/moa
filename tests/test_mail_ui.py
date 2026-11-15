"""검증 범위: 모바일 화면과 두 계정의 실제 브라우저 동작.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import os
from pathlib import Path
import subprocess
import unittest


@unittest.skipUnless(
    os.environ.get("MOA_BROWSER_TESTS") == "1",
    "set MOA_BROWSER_TESTS=1 for sandboxed Chromium integration",
)
class MailStatusUiTests(unittest.TestCase):
    def test_mobile_processing_states_filter_and_safe_filenames(self):
        subprocess.run(
            ["node", "tests/mail-status-ui.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            timeout=30,
        )

    def test_navigation_and_review_steps_on_mobile_and_desktop(self):
        subprocess.run(
            ["node", "tests/layout-ui.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            timeout=30,
        )

    def test_all_period_transactions_and_pattern_drilldowns(self):
        subprocess.run(
            ["node", "tests/patterns-ui.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            timeout=40,
        )

    def test_refresh_starts_collection_and_displays_completion_and_worker_errors(self):
        subprocess.run(
            ["node", "tests/mail-refresh-ui.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            timeout=30,
        )

    def test_installment_duplicates_recurring_and_reversible_exclusion(self):
        subprocess.run(
            ["node", "tests/finance-ui.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            timeout=40,
        )

    def test_wallet_transaction_scopes_notes_and_optional_actual_merchant_mapping(self):
        subprocess.run(
            ["node", "tests/payment-categories-ui.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            timeout=40,
        )

    def test_monthly_billing_inputs_missing_status_settings_and_mobile_layout(self):
        subprocess.run(
            ["node", "tests/billing-ui.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            timeout=40,
        )

    def test_real_owner_manager_login_conflicts_revocation_and_mobile(self):
        import tempfile
        import threading
        from werkzeug.serving import make_server, WSGIRequestHandler
        from app import app, connect
        from auth import set_password

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        old = dict(app.config)
        with tempfile.TemporaryDirectory() as directory:
            app.config.update(
                TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(directory) / "db.sqlite3")
            )
            set_password(connect, "fixture-owner-password")
            server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                subprocess.run(
                    ["node", "tests/sharing-ui.cjs"],
                    cwd=Path(__file__).resolve().parents[1],
                    env={**os.environ, "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
                    check=True,
                    timeout=55,
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                app.config.update(old)
