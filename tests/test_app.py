"""검증 범위: 파일 읽기·날짜·금액 검증과 전체 저장 흐름.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import io
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook
from auth import set_password
from app import app, previews, connect

HEADERS = {"X-Requested-With": "CardDashboard"}


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.tmp.name) / "cards.sqlite3")
        )
        self.client = app.test_client()
        set_password(connect, "fixture-login-password")
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)
        previews.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def preview(self, content, name="card.csv", header=1):
        return self.client.post(
            "/api/preview",
            data={"file": (io.BytesIO(content), name), "header": str(header)},
            headers=HEADERS,
        )

    def save(self, token, card="신한"):
        return self.client.post(
            "/api/import",
            json={"token": token, "card": card, "date": 0, "merchant": 1, "amount": 2},
            headers=HEADERS,
        )

    def data(self):
        return self.client.get("/api/data").json

    def test_csv_roundtrip_duplicate_and_delete(self):
        content = '이용일,가맹점,금액\n2026.10.01,마트,"42,000원"\n2026-10-02,마트,-12000\n2026-10-02,마트,-12000\n'.encode(
            "cp949"
        )
        token = self.preview(content).json["token"]
        self.assertEqual(self.save(token).json["count"], 3)
        data = self.data()
        self.assertEqual(sum(t["amount"] for t in data["transactions"]), 18000)
        self.assertEqual(self.save(token).status_code, 409)
        self.assertEqual(len(self.data()["transactions"]), 3)
        import_id = data["imports"][0]["id"]
        self.assertEqual(
            self.client.delete(f"/api/imports/{import_id}", headers=HEADERS).status_code, 200
        )
        self.assertEqual(self.data()["transactions"], [])
        self.assertEqual(self.save(token).status_code, 200)

    def test_xlsx_dates_and_header_offset(self):
        from datetime import datetime

        wb = Workbook()
        ws = wb.active
        ws.append(["안내"])
        ws.append(["이용일", "가맹점", "금액"])
        ws.append([datetime(2026, 10, 2), "도서점", 28000])
        out = io.BytesIO()
        wb.save(out)
        token = self.preview(out.getvalue(), "card.xlsx", 2).json["token"]
        self.assertEqual(self.save(token, "현대").status_code, 200)
        self.assertEqual(self.data()["transactions"][0]["used_on"], "2026-10-02")

    def test_shinhan_xlsx_trailing_transaction_count_is_not_a_transaction(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["거래일", "카드구분", "이용카드", "가맹점명", "승인번호", "구분", "금액"])
        for day, merchant, amount in [
            (1, "가상 식당", 10000),
            (2, "가상 상점", 20000),
            (3, "가상 빵집", 5000),
        ]:
            ws.append(
                [f"2026-10-{day:02d}", "신용", "본인****", merchant, "fixture", "일시불", amount]
            )
        ws.append([None, None, None, "총 3건", None, None, 35000])
        out = io.BytesIO()
        wb.save(out)
        result = self.preview(out.getvalue(), "shinhan.xlsx").json
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["excluded_rows"], 1)
        response = self.client.post(
            "/api/import",
            json={"token": result["token"], "card": "신한", "date": 0, "merchant": 3, "amount": 6},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["count"], 3)
        self.assertEqual(sum(row["amount"] for row in self.data()["transactions"]), 35000)

    def test_summary_label_with_transaction_date_is_not_dropped(self):
        result = self.preview(
            "날짜,가맹점,금액\n2026-10-01,합계,1000\n,총 1건,1000\n".encode()
        ).json
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.save(result["token"]).status_code, 200)
        self.assertEqual(self.data()["transactions"][0]["merchant"], "합계")
        invalid = self.preview("날짜,가맹점,금액\n2026-02-30,합계,1000\n".encode()).json
        self.assertEqual(invalid["count"], 1)
        self.assertEqual(self.save(invalid["token"]).status_code, 400)

    def test_shinhan_count_footer_with_formatting_and_other_totals(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["거래" + chr(8203) + "일", "카드구분", "이용카드", "가맹점명", "통화", "금액"])
        ws.append(["2026.10.13 13:32", "신용", "본인****", "가상 식당", "KRW", 10000])
        ws.append([None, None, None, "총" + chr(8203) + " 1건", "KRW", "10,000.00"])
        out = io.BytesIO()
        wb.save(out)
        result = self.preview(out.getvalue(), "shinhan.xlsx").json
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["excluded_rows"], 1)

    def test_explicit_last_row_and_validation_inside_selected_range(self):
        content = "날짜,가맹점,금액\n2026-10-01,상점A,1000\n2026-10-02,상점B,2000\n2026-10-03,상점C,3000\n,자료 끝,0\n".encode()
        result = self.preview(content).json
        self.assertEqual(result["count"], 4)
        payload = {
            "token": result["token"],
            "card": "신한",
            "date": 0,
            "merchant": 1,
            "amount": 2,
            "end": 3,
        }
        for end in [0, -1, 5, True, "1.5"]:
            self.assertEqual(
                self.client.post(
                    "/api/import", json={**payload, "end": end}, headers=HEADERS
                ).status_code,
                400,
            )
        self.assertEqual(
            self.client.post("/api/import", json=payload, headers=HEADERS).json["count"], 3
        )
        invalid = self.preview(
            "날짜,가맹점,금액\n2026-02-30,상점D,1000\n,자료 끝,0\n".encode()
        ).json
        self.assertEqual(
            self.client.post(
                "/api/import",
                json={**payload, "token": invalid["token"], "end": 1},
                headers=HEADERS,
            ).status_code,
            400,
        )
        self.assertEqual(len(self.data()["transactions"]), 3)

    def test_invalid_xlsx_alignment_preserves_datetime_and_values(self):
        from datetime import datetime
        from openpyxl.styles import Alignment
        from zipfile import ZipFile
        from xml.etree import ElementTree as ET

        wb = Workbook()
        ws = wb.active
        ws.append(["이용일", "가맹점", "금액"])
        ws.append([datetime(2026, 10, 2), "가상 상점", 28000])
        ws["A2"].alignment = Alignment(horizontal="left", vertical="center")
        out = io.BytesIO()
        wb.save(out)
        repaired = io.BytesIO()
        with ZipFile(io.BytesIO(out.getvalue())) as source, ZipFile(repaired, "w") as target:
            for item in source.infolist():
                data = source.read(item)
                if item.filename == "xl/styles.xml":
                    root = ET.fromstring(data)
                    next(n for n in root.iter() if n.tag.endswith("}alignment")).set(
                        "vertical", "left"
                    )
                    data = ET.tostring(root, encoding="utf-8")
                target.writestr(item, data)
        preview = self.preview(repaired.getvalue(), "kb.xlsx")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(self.save(preview.json["token"], card="KB국민").status_code, 200)
        record = self.data()["transactions"][0]
        self.assertEqual(record["used_on"], "2026-10-02")
        self.assertEqual(record["amount"], 28000)

    def test_kb_two_row_header_explicit_year_and_selected_five_rows(self):
        wb = Workbook()
        ws = wb.active
        ws.append([None, "가상 이용대금명세서"])
        ws.append(
            [
                None,
                "이용일자",
                "이용카드",
                "구분",
                "이용가맹점",
                None,
                "이용금액",
                "할부",
                "이번달결제금액",
                None,
                "잔액",
                "안내",
            ]
        )
        ws.append([None] * 8 + ["원금", "수수료"])
        for i in range(1, 6):
            ws.append(
                [
                    None,
                    f"26.09.{i:02d}",
                    "가상 카드",
                    "일시불",
                    "가상 상점 " + str(i),
                    None,
                    5000,
                    None,
                    5000,
                ]
            )
        ws.append([None, None, None, None, "집계 안내", None, None, None, None, 0])
        ws.append([None, "포인트 안내", None, None, None, None, None, None, 0])
        ws.append([None, "기타 안내", None, None, None, None, None, None, 0])
        out = io.BytesIO()
        wb.save(out)
        response = self.client.post(
            "/api/preview",
            data={
                "file": (io.BytesIO(out.getvalue()), "kb.xlsx"),
                "header": "",
                "auto_header": "1",
                "year": "2026",
            },
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        result = response.json
        self.assertEqual((result["header"], result["header_rows"], result["count"]), (2, 2, 8))
        self.assertEqual(result["columns"][8], "이번달결제금액 / 원금")
        payload = {
            "token": result["token"],
            "card": "KB국민",
            "date": 1,
            "merchant": 4,
            "amount": 6,
            "end": 5,
        }
        self.assertEqual(
            self.client.post("/api/import", json=payload, headers=HEADERS).json["count"], 5
        )
        self.assertEqual(len(self.data()["transactions"]), 5)

    def test_spreadsheet_short_year_is_never_inferred_or_mismatched(self):
        content = "이용일,가맹점,금액\n26.09.01,가상 상점,1000\n".encode()
        for year in ["", "2025"]:
            result = self.client.post(
                "/api/preview",
                data={
                    "file": (io.BytesIO(content), "kb.csv"),
                    "header": "1",
                    "auto_header": "1",
                    "year": year,
                },
                headers=HEADERS,
            ).json
            self.assertEqual(self.save(result["token"], "KB국민").status_code, 400)
        self.assertEqual(self.data()["transactions"], [])

    def test_invalid_row_rolls_back_entire_file(self):
        token = self.preview(
            "날짜,가맹점,금액\n20261001,마트,1000\n2026-02-30,식당,5000\n".encode()
        ).json["token"]
        self.assertEqual(self.save(token).status_code, 400)
        self.assertEqual(self.data()["transactions"], [])
        self.assertEqual(self.data()["imports"], [])

    def test_invalid_amount_and_mapping(self):
        for amount in ["NaN", "Infinity", "5.5", "취소", ""]:
            token = self.preview(f"날짜,가맹점,금액\n2026-10-01,마트,{amount}\n".encode()).json[
                "token"
            ]
            self.assertEqual(self.save(token).status_code, 400)
        response = self.client.post(
            "/api/import",
            json={"token": token, "card": "신한", "date": 0, "merchant": 0, "amount": 2},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 400)

    def test_request_protection_and_unsupported_file(self):
        self.assertEqual(self.client.post("/api/preview").status_code, 403)
        self.assertEqual(
            self.client.delete(
                "/api/imports/1", headers={**HEADERS, "Origin": "https://other.example"}
            ).status_code,
            403,
        )
        self.assertEqual(self.preview(b"not excel", "card.xlsx").status_code, 400)
        self.assertEqual(self.preview(b"pdf", "card.pdf").status_code, 400)
        self.assertEqual(self.preview(b"a,b,c\n", header=99).status_code, 400)

    def test_index_sample_and_empty_database(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn("이용일자", self.client.get("/sample.csv").data.decode("utf-8-sig"))
        from categories import CATEGORIES

        self.assertEqual(
            self.data(),
            {
                "transactions": [],
                "imports": [],
                "categories": CATEGORIES,
                "bank_review_summary": {
                    "pending_count": 0,
                    "pending_month": None,
                    "reflected_count": 0,
                    "selected_count": 0,
                    "unselected_count": 0,
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
