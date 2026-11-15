"""검증 범위: 브라우저 소비 분석의 계산 결과.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

from pathlib import Path
import subprocess
import unittest


class AnalyticsTests(unittest.TestCase):
    def test_refunds_weekdays_and_month_boundaries(self):
        subprocess.run(
            ["node", "tests/analytics.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            timeout=10,
        )
