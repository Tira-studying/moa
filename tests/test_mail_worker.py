"""검증 범위: 주기 수집과 즉시 수집의 실행 시점.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import unittest
from unittest.mock import patch
from mail_worker import tick


class WorkerTests(unittest.TestCase):
    @patch("mail_worker.process_refresh", return_value=None)
    def test_idle_tick_checks_requests_without_triggering_scheduled_gmail_access(self, process):
        self.assertEqual(tick(None, 1000, now=100), 1000)
        process.assert_called_once_with(None, automatic=False)

    @patch("mail_worker.time.monotonic", return_value=100)
    @patch("mail_worker.process_refresh", return_value={"ok": True, "found": 1})
    def test_manual_work_resets_the_automatic_interval(self, process, clock):
        self.assertEqual(tick(None, 1000, now=100), 400)
        process.assert_called_once_with(None, automatic=False)
