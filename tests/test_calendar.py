"""검증 범위: 사용 내역 달력 보기(날짜별 금액, 날짜 선택, 월 이동, 필터 반영, 작은 화면).

가상 거래와 임시 DB만 사용합니다. 날짜 계산 자체는 tests/analytics.cjs에서 검증합니다.
"""

import os
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import app, connect
from auth import set_password


class CalendarBrowserTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.directory.name) / "db.sqlite3")
        )
        set_password(connect, "fixture-owner-password")

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_calendar_screen(self):
        from werkzeug.serving import make_server, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        current = f"{today.year}-{today.month:02d}"
        year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        previous = f"{year}-{month:02d}"
        db = connect()
        with db:
            db.execute(
                "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(1,'신한','가상','fixture',1)"
            )
            # 이번 달 1일은 언제 실행해도 오늘 이전입니다. 지난달 10일이 첫 기록입니다.
            db.executemany(
                "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(?,?,?,?)",
                [
                    (1, f"{current}-01", "가상식당", 30_000),
                    (1, f"{current}-01", "가상카페", 5_000),
                    (1, f"{previous}-10", "가상서점", 12_000),
                ],
            )
        db.close()
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                ["node", "tests/calendar-ui.cjs"],
                cwd=Path(__file__).resolve().parents[1],
                env={
                    **os.environ,
                    "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}",
                    "MOA_TEST_MONTH": current,
                    "MOA_TEST_PREVIOUS": previous,
                },
                check=True,
                timeout=90,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
