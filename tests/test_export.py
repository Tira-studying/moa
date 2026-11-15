"""검증 범위: 사용 내역 엑셀 내보내기의 조건(기간·달력 날짜·집계·카드·분야·검색), 파일 내용,
수식 주입 방지, 본인 거래만 포함, 잘못된 조건 거절, 화면 내려받기.

가상 거래와 임시 DB만 사용합니다.
"""

import io
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import unquote

from openpyxl import load_workbook

from app import app, connect
from auth import set_password

HEADERS = {"X-Requested-With": "CardDashboard"}


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.directory.name) / "db.sqlite3")
        )
        set_password(connect, "fixture-owner-password")
        self.owner = app.test_client()
        self.owner.post("/api/login", json={"password": "fixture-owner-password"}, headers=HEADERS)
        self.owner.post(
            "/api/members",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers={
                **HEADERS,
                "If-Match": self.owner.get("/api/members").headers["X-Moa-Revision"],
            },
        )
        self.partner = app.test_client()
        self.partner.post(
            "/api/login",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers=HEADERS,
        )
        db = connect()
        with db:
            db.execute(
                "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(1,'신한','가상','a',1)"
            )
            db.execute(
                "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(2,'삼성','가상','b',2)"
            )
            db.executemany(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) VALUES(?,?,?,?,?)",
                [
                    (1, 1, "2026-10-05", "가상식당", 30_000),
                    (2, 1, "2026-11-01", "가상카페", 5_000),
                    (3, 1, "2026-11-02", '=HYPERLINK("x")', 7_000),
                    (4, 1, "2026-11-03", "가상마트", -2_000),
                    (5, 1, "2026-11-03", "가상중복", 9_000),
                    (6, 2, "2026-11-03", "다른사람상점", 1_000),
                ],
            )
            db.execute(
                "INSERT INTO transaction_annotations(transaction_id,category,actual_merchant,memo) "
                "VALUES(2,'커피','','@메모')"
            )
            db.execute("INSERT INTO transaction_exclusions(transaction_id) VALUES(5)")
        db.close()

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def export(self, client=None, **params):
        response = (client or self.owner).get("/api/export.xlsx", query_string=params)
        return response

    def rows(self, **params):
        response = self.export(**params)
        if response.status_code != 200:
            self.fail(response.get_data(as_text=True))
        sheet = load_workbook(io.BytesIO(response.data))["사용 내역"]
        values = [row for row in sheet.iter_rows(min_row=2, values_only=True)]
        return [row for row in values if row and row[0] and row[0] != "합계"], values[-1]

    def test_default_exports_active_rows_newest_first_with_total(self):
        rows, total = self.rows()
        self.assertEqual(
            [row[0] for row in rows], ["2026-11-03", "2026-11-02", "2026-11-01", "2026-10-05"]
        )
        self.assertEqual(total[:2], ("합계", "4건"))
        self.assertEqual(total[6], 40_000)
        response = self.export()
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("spreadsheetml", response.mimetype)
        self.assertIn(
            "모아-사용내역-전체기간.xlsx", unquote(response.headers["Content-Disposition"])
        )

    def test_filters_match_the_activity_screen(self):
        cases = [
            (
                {"period": "month", "month": "2026-11"},
                ["가상마트", '\'=HYPERLINK("x")', "가상카페"],
            ),
            ({"period": "day", "start": "2026-11-03"}, ["가상마트"]),
            ({"period": "custom", "start": "2026-11-02", "end": "2026-11-03"}, None),
            ({"visibility": "excluded"}, ["가상중복"]),
            ({"visibility": "all", "period": "day", "start": "2026-11-03"}, None),
            ({"category": "커피"}, ["가상카페"]),
            ({"q": "@메모"}, ["가상카페"]),
            ({"card": "삼성"}, []),
        ]
        for params, merchants in cases:
            with self.subTest(params=params):
                rows, _ = self.rows(**params)
                names = [row[2] for row in rows]
                if merchants is not None:
                    self.assertEqual(names, merchants)
        rows, _ = self.rows(period="custom", start="2026-11-02", end="2026-11-03")
        self.assertEqual(len(rows), 2)
        rows, _ = self.rows(visibility="all", period="day", start="2026-11-03")
        self.assertEqual([row[8] for row in rows], ["제외", "포함"])
        response = self.export(period="month", month="2026-11")
        self.assertIn("2026-11.xlsx", unquote(response.headers["Content-Disposition"]))

    def test_formula_like_text_stays_text_and_summary_sheet(self):
        response = self.export()
        book = load_workbook(io.BytesIO(response.data))
        rows = list(book["사용 내역"].iter_rows(min_row=2, values_only=True))
        formula = next(row for row in rows if row[0] == "2026-11-02")
        self.assertEqual(formula[1], '\'=HYPERLINK("x")')
        cafe = next(row for row in rows if row[0] == "2026-11-01")
        self.assertEqual((cafe[3], cafe[7], cafe[5]), ("커피", "'@메모", "카드"))
        summary = list(book["분야별 합계"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(summary[-1][:3], ("합계", 40_000, 4))
        self.assertIn(("커피", 5_000, 1), [row[:3] for row in summary])

    def test_only_own_rows(self):
        response = self.export(self.partner)
        rows = list(
            load_workbook(io.BytesIO(response.data))["사용 내역"].iter_rows(
                min_row=2, values_only=True
            )
        )
        self.assertEqual(rows[0][2], "다른사람상점")
        self.assertEqual(rows[-1][:2], ("합계", "1건"))
        rows, _ = self.rows(q="다른사람")
        self.assertEqual(rows, [])

    def test_invalid_conditions_are_rejected(self):
        for params in (
            {"period": "week"},
            {"period": "month", "month": "2026-13"},
            {"period": "day", "start": "2026-02-30"},
            {"period": "custom", "start": "2026-11-05", "end": "2026-11-01"},
            {"visibility": "hidden"},
            {"q": "가" * 201},
        ):
            with self.subTest(params=params):
                self.assertEqual(self.export(**params).status_code, 400)

    def test_requires_login(self):
        self.assertEqual(app.test_client().get("/api/export.xlsx").status_code, 401)


class ExportBrowserTests(unittest.TestCase):
    setUp = ExportTests.setUp
    tearDown = ExportTests.tearDown

    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_download_button(self):
        from werkzeug.serving import make_server, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                ["node", "tests/export-ui.cjs"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
                check=True,
                timeout=90,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
