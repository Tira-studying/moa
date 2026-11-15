"""검증 범위: `python manage.py demo`의 가상 데이터 채우기.

처음 써 보는 사람이 실제 명세서 없이 화면을 볼 수 있어야 하고,
이미 거래가 있는 DB(운영 DB)에는 절대 넣지 않아야 합니다.
"""

from datetime import date
import tempfile
import unittest
from pathlib import Path

from app import app
import demo_data

HEADERS = {"X-Requested-With": "CardDashboard"}


class DemoDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = dict(app.config)
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.tmp.name) / "demo.sqlite3")
        )

    def tearDown(self):
        app.config.clear()
        app.config.update(self.previous)
        self.tmp.cleanup()

    def test_fills_three_cards_through_normal_import_and_can_log_in(self):
        saved = demo_data.fill(today=date(2026, 3, 15))
        self.assertEqual([card for card, _ in saved], ["신한", "삼성", "KB국민"])
        self.assertTrue(all(count > 10 for _, count in saved))
        client = app.test_client()
        login = client.post(
            "/api/login", json={"password": demo_data.DEMO_PASSWORD}, headers=HEADERS
        )
        self.assertEqual(login.status_code, 200)
        data = client.get("/api/data").json
        self.assertEqual(len(data["imports"]), 3)
        merchants = {t["merchant"] for t in data["transactions"]}
        self.assertIn("가상 음악 구독", merchants)
        # 환불(음수)도 들어가 합계에서 빠지는지 볼 수 있습니다.
        self.assertTrue(any(t["amount"] < 0 for t in data["transactions"]))
        # 미래 날짜 거래는 만들지 않습니다.
        self.assertTrue(all(t["used_on"] <= "2026-03-15" for t in data["transactions"]))

    def test_refuses_database_that_already_has_transactions(self):
        demo_data.fill(today=date(2026, 3, 15))
        with self.assertRaises(SystemExit):
            demo_data.fill(today=date(2026, 3, 15))


if __name__ == "__main__":
    unittest.main()
