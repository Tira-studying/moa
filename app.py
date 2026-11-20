"""웹 서버의 시작점: 화면과 API를 연결하고, 입력 검증 후 거래를 저장합니다.

읽는 순서: 앱 설정 → DB 연결 → 파일 해석 → 명세서 미리보기 → 저장·조회 API.
로그인은 auth.py, 공동 수정 제어는 collaboration_access.py, 계산은 finance.py에 맡깁니다.
"""

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Request,
    jsonify,
    render_template,
    request,
    send_file,
    g,
    has_request_context,
)
from openpyxl import load_workbook
from secure_html import unlock_statement, StatementReadError, normalize_statement_tables
from secure_pdf import unlock_pdf
from auth import install_auth
from categories import (
    merchant_key,
    classify,
    payment_service,
    category_catalog,
    category_labels,
    category_report,
    save_category,
    delete_category,
    category_review_summary,
)
import finance
import personal_data
import sms_inbox
import codef_connection
import codef_sync
import codef_bank
import bank_expenses
import connection_setup
import migrations
import card_performance
import features
import tax_deduction
import budgets
import subscriptions
import foreign_payments
import manual_entries
import receipts
import shared_spending
import export_xlsx
from preview_store import PreviewStore
from personal_data import current_user_id
from collaboration_access import install_collaboration


# 1. 업로드와 앱 설정: 원본 파일은 메모리에서 처리합니다.
# ─────────────────────────────────────────────────────────────────
class InMemoryRequest(Request):
    """업로드 파일이 임시 디스크 파일로 넘어가지 않도록 메모리 스트림을 사용합니다."""

    def _get_file_stream(
        self, total_content_length, content_type, filename=None, content_length=None
    ):
        # 큰 업로드도 임시 디스크 파일로 넘어가지 않게 메모리 스트림을 반환합니다.
        return io.BytesIO()


app = Flask(__name__)
app.request_class = InMemoryRequest
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["DATABASE"] = os.environ.get(
    "CARD_DB", str(Path(__file__).parent / "instance" / "cards.sqlite3")
)
app.config["PUBLIC_ORIGIN"] = os.environ.get("MOA_PUBLIC_ORIGIN", "").rstrip("/")
app.config["TRUST_PROXY"] = os.environ.get("MOA_TRUST_PROXY") == "1"
CARDS = ["신한", "삼성", "현대", "KB국민", "우리", "롯데"]
# DB 저장 전의 임시 표입니다. 메모리에만 두며 10분이 지나거나 서버를 재시작하면 사라집니다.
# previews: 열을 고르기 직전의 거래 표. secure_tables: 암호를 해제한 명세서의 표 묶음(더 민감해서 적게 보관).
PREVIEW_SECONDS = 600
previews = PreviewStore(ttl=PREVIEW_SECONDS, per_owner=10, total=30)
secure_tables = PreviewStore(ttl=PREVIEW_SECONDS, per_owner=3, total=10)


# 2. 공통 저장소와 오류 응답
# ─────────────────────────────────────────────────────────────────
def connect():
    """요청 전체의 트랜잭션이 있으면 연결을 재사용하고, 그 외에는 새 연결을 엽니다.

    연결마다 DB 구조 버전(user_version)만 확인하고, 최신이 아닐 때만 migrations.py의 남은 단계를
    실행합니다. 테이블 구조를 바꾸려면 migrations.py의 안내를 따르세요.
    """
    if has_request_context() and getattr(g, "shared_db", None) is not None:
        return g.shared_db
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(app.config["DATABASE"])
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        migrations.migrate(db)
    except Exception:
        db.close()
        raise
    return db


def fail(message, status=400):
    """화면에서 읽을 수 있는 오류 메시지와 HTTP 상태 코드를 JSON으로 반환합니다."""
    return jsonify(error=message), status


install_auth(app, connect)
install_collaboration(app, connect)
sms_inbox.install(app, connect)
codef_connection.install(app, connect)
codef_sync.install(app, connect)
codef_bank.install(app, connect)
bank_expenses.install(app, connect)
connection_setup.install(app, connect)


@app.errorhandler(413)
def too_large(_):
    """업로드 크기 제한을 넘었을 때 공통 오류 형식을 반환합니다."""
    if request.endpoint == "sms_receive":
        return fail("문자 전송 요청은 8KB 이하로 보내 주세요.", 413)
    return fail("파일은 8MB 이하로 올려 주세요.", 413)


# 3. 날짜·금액·CSV·XLSX 해석
# ─────────────────────────────────────────────────────────────────
def parse_date(value):
    """연도가 포함된 날짜를 YYYY-MM-DD 문자열로 바꿉니다. 연도는 추측하지 않습니다."""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    text = re.sub(r"\s*([./-])\s*", r"\1", str(value or "").strip())
    # 명세서 발행 시점으로 연도를 추측하지 않고 네 자리 연도를 요구합니다.
    match = re.fullmatch(
        r"(\d{4})[-./년\s] *(\d{1,2})[-./월\s] *(\d{1,2})(?:일)?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?",
        text,
    )
    if not match and re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    if not match:
        raise ValueError("날짜는 연도를 포함한 YYYY-MM-DD 형식이어야 합니다.")
    return date(*map(int, match.groups())).isoformat()


def parse_statement_date(value, year=None):
    """연도가 생략된 명세서 날짜에는 사용자가 지정한 연도만 적용합니다."""
    try:
        return parse_date(value)
    except ValueError:
        if year is None:
            raise ValueError(
                "이용일에 연도가 없거나 날짜 형식이 다릅니다. 연도가 없다면 HTML 열기의 적용 연도를 지정해 주세요."
            ) from None
        text = re.sub(r"\s*([./-])\s*", r"\1", str(value or "").strip())
        short = re.fullmatch(r"(?:(\d{2})[-./])?(\d{1,2})[-./](\d{1,2})", text)
        if not short:
            raise ValueError("이용일 형식을 확인해 주세요.") from None
        yy, month, day = short.groups()
        if yy is not None and int(yy) != year % 100:
            raise ValueError("입력한 적용 연도와 이용일의 두 자리 연도가 다릅니다.")
        used_on = date(year, int(month), int(day))
        # 명세서에는 미래 이용일이 없습니다. 적용 연도를 잘못 넣으면(예: 2027) 저장 전에 멈춥니다.
        if used_on > datetime.now(ZoneInfo("Asia/Seoul")).date():
            raise ValueError(
                f"적용 연도 {year}년으로 만든 이용일 {used_on.isoformat()}이 오늘 이후입니다. "
                "적용 연도를 확인해 주세요."
            )
        return used_on.isoformat()


def parse_amount(value):
    """쉼표·통화 표시가 있는 금액을 정수로 바꾸고 해석할 수 없는 값은 거절합니다."""
    text = (
        str(value if value is not None else "")
        .strip()
        .replace(",", "")
        .replace("₩", "")
        .replace("원", "")
        .strip()
    )
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        number = Decimal(text)
        if not number.is_finite() or number != number.to_integral_value() or abs(number) > 10**12:
            raise ValueError()
        return int(number)
    except (InvalidOperation, ValueError):
        raise ValueError("금액은 원 단위 숫자여야 합니다. 취소·환불은 음수로 입력해 주세요.")


def compatible_xlsx(data):
    """일부 KB 엑셀의 잘못된 정렬 속성만 메모리에서 보정해 읽을 수 있게 합니다."""
    import zipfile
    from xml.etree import ElementTree

    with zipfile.ZipFile(io.BytesIO(data)) as source:
        if sum(item.file_size for item in source.infolist()) > 50 * 1024 * 1024:
            raise ValueError("압축 해제 크기가 너무 큰 엑셀 파일입니다.")
        if "xl/styles.xml" not in source.namelist():
            return data
        root = ElementTree.fromstring(source.read("xl/styles.xml"))
        changed = False
        for node in root.iter():
            if (
                node.tag.rsplit("}", 1)[-1] == "alignment"
                and "vertical" in node.attrib
                and node.attrib["vertical"]
                not in {"top", "center", "bottom", "justify", "distributed"}
            ):
                del node.attrib["vertical"]
                changed = True
        if not changed:
            return data
        output = io.BytesIO()
        styles = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        with zipfile.ZipFile(output, "w") as target:
            for item in source.infolist():
                target.writestr(
                    item, styles if item.filename == "xl/styles.xml" else source.read(item)
                )
        return output.getvalue()


def read_rows(data, filename):
    """CSV 또는 XLSX 파일 바이트를 행과 셀 값의 목록으로 바꿉니다."""
    if filename.lower().endswith(".csv"):
        decoded = None
        for encoding in ("utf-8-sig", "cp949"):
            try:
                decoded = data.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
        if decoded is None:
            raise ValueError("CSV 인코딩을 읽을 수 없습니다. UTF-8 또는 CP949로 저장해 주세요.")
        try:
            dialect = csv.Sniffer().sniff(decoded[:8192], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = []
        for row in csv.reader(io.StringIO(decoded), dialect):
            rows.append(row)
            if len(rows) > 20000:
                raise ValueError("한 파일에 최대 20,000행까지 지원합니다.")
        return rows
    if filename.lower().endswith(".xlsx"):
        try:
            compatible = compatible_xlsx(data)
            workbook = load_workbook(io.BytesIO(compatible), read_only=True, data_only=True)
            try:
                rows = []
                for row in workbook.worksheets[0].iter_rows(values_only=True):
                    rows.append(list(row))
                    if len(rows) > 20000 or len(row) > 100:
                        raise ValueError("엑셀은 최대 20,000행·100열까지 지원합니다.")
                return rows
            finally:
                workbook.close()
        except ValueError:
            raise
        except Exception:
            raise ValueError("엑셀을 읽을 수 없습니다. 암호를 해제하고 .xlsx로 다시 저장해 주세요.")
    raise ValueError("CSV 또는 XLSX 파일을 선택해 주세요. PDF·XLS는 아직 지원하지 않습니다.")


# 4. 이메일 명세서 목록·수집·검토 API
# ─────────────────────────────────────────────────────────────────
@app.get("/api/mail")
def mail_inbox():
    """수집된 이메일 명세서 목록과 처리 상태를 화면에 반환합니다."""
    from gmail_inbox import inbox

    return jsonify(inbox(connect))


@app.post("/api/mail/refresh")
def mail_refresh():
    """메일 수집 요청을 대기열에 넣습니다. 실제 Gmail 연결은 수집 프로세스가 담당합니다."""
    from gmail_inbox import request_refresh, MailError

    try:
        return jsonify(ticket=request_refresh(connect)), 202
    except (MailError, OSError) as exc:
        return fail(
            str(exc) if isinstance(exc, MailError) else "Gmail 설정 파일을 읽지 못했습니다."
        )


@app.post("/api/mail/<identifier>/attachment")
def mail_attachment(identifier):
    """선택한 이메일 첨부파일을 메모리로 읽어 다운로드 응답을 만듭니다."""
    from gmail_inbox import attachment, MailError

    try:
        item = attachment(connect, identifier)
        return send_file(
            io.BytesIO(item["data"]),
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=item["filename"],
        )
    except (MailError, OSError) as exc:
        return fail(str(exc) if isinstance(exc, MailError) else "Gmail 연결 설정을 확인해 주세요.")


@app.post("/api/mail/<identifier>/prepare")
def mail_prepare(identifier):
    """NAS의 공통 암호로 선택한 첨부를 열고, 현재 사용자의 검토용 표를 준비합니다."""
    from gmail_inbox import attachment, common_password, open_statement, mark_prepared, MailError

    try:
        password = common_password()
        if password is None:
            raise MailError("NAS에 공통 명세서 비밀번호를 먼저 설정해 주세요.")
        payload = request.get_json(silent=True) or {}
        card = payload.get("card")
        if card not in {"삼성", "현대", "롯데", "우리", "신한"}:
            raise MailError("지원하는 보안 명세서 카드사를 선택해 주세요.")
        year_text = str(payload.get("year") or "").strip()
        year = int(year_text) if year_text else None
        if year is not None and not 1900 <= year <= 2100:
            raise MailError("적용 연도는 1900~2100 사이로 입력해 주세요.")
        item = attachment(connect, identifier)
        item["card"] = card
        try:
            tables = open_statement(item, password)
        except (ValueError, OSError):
            from gmail_inbox import mark_failed

            mark_failed(connect, hashlib.sha256(item["data"]).hexdigest())
            raise
        mark_prepared(connect, identifier, card, tables)
        return secure_result(item["data"], tables, card, item["filename"], year)
    except StatementReadError as exc:
        return jsonify(error=str(exc), structure=exc.structure), 400
    except (ValueError, OSError) as exc:
        return fail(
            str(exc) if isinstance(exc, ValueError) else "공통 비밀번호 설정을 확인해 주세요."
        )
    finally:
        password = ""


@app.post("/api/mail/<identifier>/check")
def mail_check(identifier):
    """일괄검토 중 첨부 하나의 판독 가능 여부를 확인합니다. 거래를 저장하지는 않습니다."""
    from gmail_inbox import (
        attachment,
        common_password,
        open_statement,
        mark_prepared,
        mark_failed,
        MailError,
    )

    password = ""
    item = None
    try:
        payload = request.get_json(silent=True) or {}
        card = payload.get("card")
        if card not in CARDS:
            raise MailError("카드사를 먼저 선택해 주세요.")
        item = attachment(connect, identifier)
        item["card"] = card
        suffix = Path(item["filename"]).suffix.lower()
        if suffix in {".csv", ".xlsx"}:
            rows = read_rows(item["data"], item["filename"])
            header, header_rows, columns = spreadsheet_header(rows, 1, True)
            if not 3 <= len(columns) <= 100:
                raise MailError("날짜·가맹점·금액을 포함한 제목 행을 확인해 주세요.")
            body = [
                r
                for r in rows[header + header_rows - 1 :]
                if any(v is not None and str(v).strip() for v in r)
            ]
            body, _ = strip_summary_footer(body, columns)
            if not body:
                raise MailError("거래 후보가 없습니다. 파일을 직접 검토해 주세요.")
            tables = [[columns, *body]]
        else:
            password = common_password()
            if password is None:
                raise MailError("일괄검토하려면 NAS에 공통 명세서 비밀번호를 설정해 주세요.")
            tables = open_statement(item, password)
        tables = normalize_statement_tables(tables, card)
        mark_prepared(connect, identifier, card, tables)
        return jsonify(
            ok=True, count=sum(max(0, len(table) - 1) for table in tables), tables=len(tables)
        )
    except (ValueError, OSError, csv.Error) as exc:
        if item is not None:
            mark_failed(connect, hashlib.sha256(item["data"]).hexdigest())
        return fail(
            str(exc)
            if isinstance(exc, (ValueError, csv.Error))
            else "첨부파일 또는 비밀번호 설정을 확인해 주세요."
        )
    finally:
        password = ""


# PWA의 서비스 워커는 로그인 전에도 설치할 수 있어야 합니다.
# 루트 경로에서 제공해야 로그인 화면과 모든 앱 화면을 같은 범위로 관리할 수 있습니다.
@app.get("/sw.js")
def pwa_service_worker():
    """금융 내역을 저장하지 않는 서비스 워커를 앱 전체 범위로 제공합니다."""
    response = send_file(Path(app.static_folder) / "sw.js", mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/")
def index():
    """로그인을 통과한 사용자에게 모아의 기본 HTML 화면을 반환합니다."""
    return render_template("index.html", user_id=g.user["id"])


# 5. 표 정리와 저장 전 미리보기
# ─────────────────────────────────────────────────────────────────
def strip_summary_footer(rows, columns):
    """명확한 마지막 합계 행만 제외하고, 불명확한 행은 검증을 위해 남겨둡니다."""
    compact_text = lambda value: re.sub(
        r"[\s\u200b\ufeff]+", "", unicodedata.normalize("NFKC", str(value))
    )
    date_index = next(
        (
            i
            for i, c in enumerate(columns)
            if re.search(
                r"이용일|사용일|거래일|승인일|매출일|결제일|날짜|일자|date", compact_text(c), re.I
            )
        ),
        None,
    )
    if date_index is None:
        return rows, 0
    label = re.compile(
        r"(?:총[\d,]+건|총계|합계|소계|이용합계|이용금액합계|금액합계|합계금액)(?:[:：])?"
    )

    def summary(row):
        values = [str(v).strip() for v in row if v is not None and str(v).strip()]
        compact = [compact_text(v) for v in values]
        date_cell = row[date_index] if date_index < len(row) else None
        date_value = "" if date_cell is None else str(date_cell).strip()
        normalized_date = compact_text(date_value)
        if normalized_date and not label.fullmatch(normalized_date):
            return False
        if any(re.fullmatch(r"총[\d,]+건[:：]?", v) for v in compact):
            return True
        return any(label.fullmatch(v) for v in compact) and all(
            label.fullmatch(v) or re.fullmatch(r"[₩+\-(]?[\d,]+(?:원|건|\))?", v) for v in compact
        )

    end = len(rows)
    while end and summary(rows[end - 1]):
        end -= 1
    return rows[:end], len(rows) - end


def spreadsheet_header(rows, requested, automatic=False):
    """제목 행을 결정하고 여러 줄로 나뉜 열 제목을 합쳐 반환합니다."""
    compact = lambda v: re.sub(
        r"[\s\u200b\ufeff]+", "", unicodedata.normalize("NFKC", str(v or ""))
    )
    if automatic:
        patterns = [
            r"이용일|사용일|거래일|승인일|매출일|결제일|날짜|일자|date",
            r"가맹점|이용처|사용처|상호|업체명|merchant",
            r"금액|이용액|사용액|청구액|원화|amount",
        ]
        requested = None
        for n, row in enumerate(rows[:30], 1):
            indices = [
                next((i for i, v in enumerate(row) if re.search(pattern, compact(v), re.I)), None)
                for pattern in patterns
            ]
            if all(i is not None for i in indices) and len(set(indices)) == 3:
                requested = n
                break
        if requested is None:
            raise ValueError(
                "제목 행을 자동으로 찾지 못했습니다. 자동 찾기를 끄고 제목 행 번호를 지정해 주세요."
            )
    if not 1 <= requested <= len(rows):
        raise ValueError("열 제목이 있는 행 번호를 확인해 주세요.")
    height = 1
    if automatic:
        for row in rows[requested : requested + 2]:
            values = [compact(v) for v in row if v is not None and str(v).strip()]
            if values and all(
                re.fullmatch(
                    r"원금|수수료|청구원금|할부수수료|회차|이자|원화|외화|금액|할인금액", v
                )
                for v in values
            ):
                height += 1
            else:
                break
    selected = rows[requested - 1 : requested - 1 + height]
    width = max(map(len, selected))
    columns = [
        " / ".join(
            dict.fromkeys(
                str(row[i]).strip()
                for row in selected
                if i < len(row) and row[i] is not None and str(row[i]).strip()
            )
        )
        for i in range(width)
    ]
    return requested, height, columns


@app.post("/api/preview")
def preview():
    """업로드한 CSV·XLSX를 검사하고, 저장 전 확인할 표와 임시 토큰을 반환합니다."""
    upload = request.files.get("file")
    if not upload:
        return fail("파일을 선택해 주세요.")
    data = upload.read()
    try:
        automatic = request.form.get("auto_header") == "1"
        header = 1 if automatic else int(request.form.get("header", "1"))
        rows = read_rows(data, upload.filename or "")
        header, header_rows, columns = spreadsheet_header(rows, header, automatic)
        year_text = request.form.get("year", "").strip()
        year = int(year_text) if year_text else None
        if year is not None and not 1900 <= year <= 2100:
            raise ValueError("적용 연도는 1900~2100 사이로 입력해 주세요.")
        if not 3 <= len(columns) <= 100:
            raise ValueError("날짜·가맹점·금액을 포함한 3~100개 열이 필요합니다.")
        body = [
            r
            for r in rows[header + header_rows - 1 :]
            if any(v is not None and str(v).strip() for v in r)
        ]
        body, excluded_rows = strip_summary_footer(body, columns)
        if not body:
            raise ValueError("제목 행 아래에 사용 내역이 없습니다. 합계 행만 있는지 확인해 주세요.")
        from gmail_inbox import mark_read

        mark_read(connect, hashlib.sha256(data).hexdigest())
        token = previews.put(
            g.user["id"],
            dict(
                rows=body,
                columns=columns,
                filename=Path(upload.filename).name,
                digest=hashlib.sha256(data).hexdigest(),
                year=year,
            ),
        )
        return jsonify(
            token=token,
            columns=columns,
            count=len(body),
            excluded_rows=excluded_rows,
            header=header,
            header_rows=header_rows,
            rows=[
                [v.isoformat() if isinstance(v, (date, datetime)) else str(v or "") for v in row]
                for row in body[:5]
            ],
        )
    except (ValueError, csv.Error) as exc:
        return fail(str(exc))


def expire_preview(token):
    """미리보기 토큰을 두 보관소에서 모두 지웁니다. 자동 만료는 PreviewStore가 맡습니다."""
    previews.discard(token)
    secure_tables.discard(token)


@app.after_request
def sensitive_response_headers(response):
    """API 응답이 브라우저 캐시에 남지 않도록 응답 헤더를 지정합니다."""
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# 6. 암호 명세서 판독과 사용자별 임시 결과
# ─────────────────────────────────────────────────────────────────
def secure_result(data, tables, card, filename, year):
    """해제한 표를 정리하고 사용자별 미리보기를 만듭니다. 중복 판정 해시는 정리 전 표로 유지합니다."""
    table_hashes = [
        hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        for rows in tables
    ]
    tables = normalize_statement_tables(tables, card)
    from gmail_inbox import mark_read

    mark_read(connect, hashlib.sha256(data).hexdigest())
    token = secure_tables.put(
        g.user["id"],
        dict(
            tables=tables,
            table_hashes=table_hashes,
            year=year,
            card=card,
            filename=Path(filename).name,
            digest=hashlib.sha256(data).hexdigest(),
        ),
    )
    return jsonify(
        token=token,
        tables=[
            dict(
                index=i,
                count=len(rows),
                rows=rows[:5],
                suggested_header=next(
                    (
                        n + 1
                        for n, r in enumerate(rows[:10])
                        if any(re.search("이용일|사용일|거래일|승인일|날짜", c) for c in r)
                    ),
                    1,
                ),
            )
            for i, rows in enumerate(tables)
        ],
    )


@app.post("/api/secure-html")
def secure_html():
    """보안 HTML 또는 PDF를 판독기에 전달하고 저장 전 검토용 결과를 반환합니다."""
    upload = request.files.get("file")
    password = request.form.get("password", "")
    if not upload or not (upload.filename or "").lower().endswith((".html", ".htm", ".pdf")):
        return fail("삼성·롯데·현대·우리 HTML 또는 신한 PDF 파일을 선택해 주세요.")
    try:
        card = request.form.get("card", "삼성")
        if card not in ("삼성", "롯데", "현대", "우리", "신한"):
            raise ValueError("명세서 카드사는 삼성·롯데·현대·우리·신한을 선택해 주세요.")
        is_pdf = (upload.filename or "").lower().endswith(".pdf")
        if is_pdf != (card == "신한"):
            raise ValueError("신한카드는 PDF, 삼성·롯데·현대·우리카드는 HTML 파일을 선택해 주세요.")
        data = upload.read()
        year_text = request.form.get("year", "").strip()
        year = int(year_text) if year_text else None
        if year is not None and not 1900 <= year <= 2100:
            raise ValueError("적용 연도는 1900~2100 사이로 입력해 주세요.")
        try:
            tables = (
                unlock_pdf(data, password)
                if is_pdf
                else unlock_statement(
                    data,
                    password,
                    card=card,
                    **({"allow_online": True} if request.form.get("allow_online") == "1" else {}),
                )
            )
        except (ValueError, OSError):
            from gmail_inbox import mark_failed

            mark_failed(connect, hashlib.sha256(data).hexdigest())
            raise
        return secure_result(data, tables, card, upload.filename, year)
    except StatementReadError as exc:
        return jsonify(error=str(exc), structure=exc.structure), 400
    except ValueError as exc:
        return fail(str(exc))
    finally:
        # Request bodies live only for this request; do not log or persist them.
        password = ""


@app.post("/api/secure-preview")
def secure_preview():
    """현재 사용자가 고른 표·제목 행·거래 범위를 일반 가져오기 형식으로 바꿉니다."""
    payload = request.get_json(silent=True) or {}
    source = secure_tables.get_for(payload.get("token"), g.user["id"])
    if not source:
        return fail("보안 미리보기가 만료되었습니다. 파일을 다시 열어 주세요.")
    db = connect()
    try:
        legacy = db.execute(
            "SELECT 1 FROM imports WHERE card=? AND digest=? AND owner_id=?",
            (source.get("card", "삼성"), source["digest"], current_user_id()),
        ).fetchone()
    finally:
        db.close()
    if legacy:
        return fail(
            "이 파일은 이전 버전으로 이미 등록되어 있습니다. 중복 집계를 막기 위해 기존 등록 내역을 확인한 뒤 필요하면 삭제하고 다시 가져와 주세요.",
            409,
        )
    try:
        table = int(payload.get("table", -1))
        if table < 0 or table >= len(source["tables"]):
            raise ValueError()
        rows = source["tables"][table]
        header, end = int(payload.get("header", 1)), int(payload.get("end", len(rows)))
        if not 1 <= header < end <= len(rows):
            raise ValueError()
        columns = rows[header - 1]
        if not 3 <= len(columns) <= 100:
            raise ValueError()
        body = rows[header:end]
        table_hash = (
            source["table_hashes"][table]
            if "table_hashes" in source
            else hashlib.sha256(
                json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
        )
        token = previews.put(
            g.user["id"],
            dict(
                rows=body,
                columns=columns,
                filename=f"{source['filename']} · 내역 {table+1}",
                digest=source["digest"] + ":" + table_hash,
                card=source.get("card", "삼성"),
                year=source.get("year"),
            ),
        )
        return jsonify(
            token=token,
            columns=columns,
            count=len(body),
            rows=body[:5],
            card=source.get("card", "삼성"),
        )
    except (TypeError, ValueError):
        return fail("표와 제목 행·마지막 거래 행 번호를 확인해 주세요.")


# 7. 거래 저장·분류·조회
# ─────────────────────────────────────────────────────────────────
class ImportRequestError(ValueError):
    """저장 요청의 열·행 지정이 잘못됐을 때 화면에 보여줄 메시지를 담습니다."""


def is_whole_number(value):
    """True/False가 아닌 0 이상의 정수 표기인지 확인합니다. (JSON의 true는 int로 바뀌므로 따로 막습니다.)"""
    return not isinstance(value, bool) and re.fullmatch(r"[0-9]+", str(value)) is not None


def required_columns(payload, column_count):
    """날짜·가맹점·금액 열 번호를 읽습니다. 세 열은 서로 달라야 하고 미리보기 범위 안이어야 합니다."""
    try:
        indices = [int(payload[k]) for k in ("date", "merchant", "amount")]
    except (KeyError, ValueError, TypeError):
        indices = None
    if (
        indices is None
        or len(set(indices)) != 3
        or any(i < 0 or i >= column_count for i in indices)
    ):
        raise ImportRequestError("날짜·가맹점·금액에 서로 다른 열을 지정해 주세요.")
    return indices


def actual_merchant_column(payload, column_count, indices):
    """선택 사항인 '실제 가맹점' 열 번호를 읽습니다. 지정하지 않았으면 None입니다."""
    raw = payload.get("actual_merchant")
    if raw is None or raw == "":
        return None
    if not is_whole_number(raw) or int(raw) >= column_count or int(raw) in indices:
        raise ImportRequestError("실제 가맹점은 날짜·가맹점·금액과 다른 열을 선택해 주세요.")
    return int(raw)


def last_row_number(payload, row_count):
    """저장할 마지막 데이터 행(1부터 시작)을 읽습니다. 합계·안내 행을 빼고 저장할 때 씁니다."""
    raw = payload.get("end", row_count)
    if not isinstance(raw, bool):
        raw = str(raw).strip()
    if not is_whole_number(raw) or not 1 <= int(raw) <= row_count:
        raise ImportRequestError("마지막 거래 행은 미리보기의 데이터 행 범위 안에서 지정해 주세요.")
    return int(raw)


@app.post("/api/import")
def import_file():
    """선택한 모든 거래를 검증한 뒤 한 번에 저장합니다. 한 행이라도 잘못되면 저장하지 않습니다."""
    payload = request.get_json(silent=True) or {}
    source = previews.get_for(payload.get("token"), g.user["id"])
    if not source:
        return fail("미리보기가 만료되었습니다. 파일을 다시 선택해 주세요.")
    card = payload.get("card")
    if source.get("card") and card != source["card"]:
        return fail("보안 명세서를 연 카드사와 같은 카드사로 저장해 주세요.")
    if card not in CARDS:
        return fail("카드사를 선택해 주세요.")
    try:
        indices = required_columns(payload, len(source["columns"]))
        actual_index = actual_merchant_column(payload, len(source["columns"]), indices)
        end = last_row_number(payload, len(source["rows"]))
    except ImportRequestError as exc:
        return fail(str(exc))
    records, errors, actual_merchants = [], [], []
    for n, row in enumerate(source["rows"][:end], 1):
        try:
            values = [row[i] if i < len(row) else None for i in indices]
            merchant = str(values[1] or "").strip()
            if not merchant or len(merchant) > 500:
                raise ValueError("가맹점 이름이 비어 있거나 너무 깁니다.")
            used_on = (
                parse_statement_date(values[0], source.get("year"))
                if source.get("card") or source.get("year") is not None
                else parse_date(values[0])
            )
            actual = (
                str("" if row[actual_index] is None else row[actual_index]).strip()
                if actual_index is not None and actual_index < len(row)
                else ""
            )
            if len(actual) > 500:
                raise ValueError("실제 가맹점은 500자 이하여야 합니다.")
            records.append((used_on, merchant, parse_amount(values[2])))
            actual_merchants.append(actual)
        except ValueError as exc:
            if len(errors) < 5:
                errors.append(f"데이터 {n}행: {exc}")
    if errors:
        return fail(
            "저장하지 않았습니다. 합계·안내 행을 제거하거나 값을 수정해 주세요. "
            + " / ".join(errors)
        )
    db = connect()
    try:
        with db:
            cursor = db.execute(
                "INSERT INTO imports(card,filename,digest,owner_id) VALUES(?,?,?,?)",
                (card, source["filename"], source["digest"], current_user_id()),
            )
            db.executemany(
                "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(?,?,?,?)",
                [(cursor.lastrowid, *r) for r in records],
            )
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM transactions WHERE import_id=? ORDER BY id", (cursor.lastrowid,)
                )
            ]
            db.executemany(
                "INSERT INTO transaction_annotations(transaction_id,actual_merchant) VALUES(?,?)",
                [(row_id, name) for row_id, name in zip(ids, actual_merchants) if name],
            )
            duplicate_count = finance.new_duplicate_count(db, cursor.lastrowid)
    except sqlite3.IntegrityError:
        return fail("이 카드사에 이미 등록한 파일입니다. 중복 저장하지 않았습니다.", 409)
    finally:
        db.close()
    g.audit_target = f"명세서 #{cursor.lastrowid}"
    return jsonify(count=len(records), duplicate_count=duplicate_count)


# 계좌 지출은 imports.digest가 bank_expenses.SOURCE_PREFIX로, 직접 입력 지출은 manual_entries.SOURCE_PREFIX로
# 시작합니다. 그 외는 카드 거래입니다.
PAYMENT_KIND_SQL = (
    f"CASE WHEN i.digest LIKE '{bank_expenses.SOURCE_PREFIX}%' THEN 'bank' "
    f"WHEN i.digest LIKE '{manual_entries.SOURCE_PREFIX}%' THEN 'manual' ELSE 'card' END"
)

# 본인 거래 전체와 분류·메모·검토·제외 표시를 한 번에 읽습니다.
TRANSACTIONS_SQL = f"""
    SELECT t.*, i.card, {PAYMENT_KIND_SQL} AS payment_kind,
           i.card_product_id AS import_card_product,
           a.category AS transaction_category,
           COALESCE(a.actual_merchant, '') AS actual_merchant,
           COALESCE(a.memo, '') AS memo,
           CASE WHEN cr.transaction_id IS NULL THEN 0 ELSE 1 END AS category_reviewed,
           CASE WHEN e.transaction_id IS NULL THEN 0 ELSE 1 END AS excluded,
           COALESCE(e.reason, '') AS excluded_reason,
           CASE WHEN sh.transaction_id IS NULL THEN 0 ELSE 1 END AS shared_hidden,
           m.method AS manual_method, COALESCE(m.cash_receipt, 0) AS cash_receipt,
           COALESCE((SELECT GROUP_CONCAT(r.id) FROM transaction_receipts r
                     WHERE r.transaction_id = t.id), '') AS receipt_ids,
           COALESCE(fx.currency, '') AS foreign_currency, COALESCE(fx.amount, '') AS foreign_amount,
           COALESCE(fx.rate, '') AS foreign_rate, COALESCE(fx.confirmed, 1) AS foreign_confirmed
    FROM transactions t
    JOIN imports i ON i.id = t.import_id
    LEFT JOIN manual_entries m ON m.transaction_id = t.id
    LEFT JOIN foreign_amounts fx ON fx.transaction_id = t.id
    LEFT JOIN shared_hidden sh ON sh.transaction_id = t.id
    LEFT JOIN transaction_exclusions e ON e.transaction_id = t.id
    LEFT JOIN transaction_annotations a ON a.transaction_id = t.id
    LEFT JOIN category_reviews cr ON cr.transaction_id = t.id
    WHERE i.owner_id = ?
    ORDER BY used_on DESC, t.used_time DESC, t.id DESC
"""

# 저장 기록(명세서·계좌 지출 묶음)별 거래 수입니다.
IMPORTS_SQL = f"""
    SELECT i.id, i.card, i.filename, i.created, {PAYMENT_KIND_SQL} AS payment_kind,
           COUNT(t.id) AS count
    FROM imports i
    LEFT JOIN transactions t ON t.import_id = i.id
    WHERE i.owner_id = ?
    GROUP BY i.id
    ORDER BY i.id DESC
"""


def apply_category(row, overrides, labels):
    """거래 한 건의 분류와 분류 출처를 정합니다.

    우선순위: 거래별 직접 지정 → 가맹점 공통 지정(overrides) → 가맹점 이름 자동 분류.
    간편결제(payment_service)는 실제 가맹점을 알 수 없어서 거래별로만 분류합니다.
    """
    row["category_reviewed"] = bool(row["category_reviewed"])
    row["payment_service"] = payment_service(row["merchant"]) or payment_service(
        row["actual_merchant"]
    )
    if row["transaction_category"] is not None:
        row["category"], row["category_source"] = row["transaction_category"], "transaction"
    else:
        row["category"], row["category_source"] = classify(
            row["actual_merchant"] or row["merchant"], overrides
        )
    row["category_scope"] = (
        "transaction"
        if row["payment_service"] or row["transaction_category"] is not None
        else "merchant"
    )
    # 내부 분류 키를 사용자가 바꾼 이름으로 표시합니다. 카드 실적 제외 분야 비교에는 키를 씁니다.
    row["category_key"] = row["category"]
    row["category"] = labels.get(row["category"], row["category"])


def classified_transactions(db, user_id):
    """본인 거래 전체에 분류를 적용해 돌려줍니다. 분류 이름 사전(labels)도 함께 돌려줍니다."""
    transactions = [dict(r) for r in db.execute(TRANSACTIONS_SQL, (user_id,))]
    overrides = dict(
        db.execute(
            "SELECT merchant_key,category FROM merchant_categories WHERE owner_id=?",
            (user_id,),
        )
    )
    labels = category_labels(db, user_id)
    for row in transactions:
        apply_category(row, overrides, labels)
    return transactions, labels


@app.get("/api/data")
def get_data():
    """거래·저장 기록·분류를 조회하고, 직접 지정한 분류와 자동 분류의 우선순위를 적용합니다."""
    user_id = current_user_id()
    db = connect()
    try:
        transactions, labels = classified_transactions(db, user_id)
        imports = [dict(r) for r in db.execute(IMPORTS_SQL, (user_id,))]
        return jsonify(
            transactions=transactions,
            imports=imports,
            categories=list(labels.values()),
            bank_review_summary=bank_expenses.review_summary(db, user_id),
        )
    finally:
        db.close()


@app.put("/api/transactions/<int:transaction_id>/category")
def update_category(transaction_id):
    """거래별 분류·실제 가맹점·메모 또는 같은 가맹점의 공통 분류를 수정합니다."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or "category" not in payload:
        return fail("분야를 선택해 주세요.")
    category = payload["category"]
    update = payload.get("update_category", True)
    if type(update) is not bool:
        return fail("분류 변경 여부를 확인해 주세요.")
    # 선택 항목: 소비에서 제외(카드 대금 이체 등), 함께 보기에서 상대에게 숨기기. 없으면 그대로 둡니다.
    not_spending = payload.get("not_spending")
    shared_hidden = payload.get("shared_hidden")
    if any(
        value is not None and type(value) is not bool for value in (not_spending, shared_hidden)
    ):
        return fail("소비 제외·숨기기 여부를 확인해 주세요.")
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if category is not None:
                names = {row["name"]: row["key"] for row in category_catalog(db, current_user_id())}
                if not isinstance(category, str) or category not in names:
                    return fail("내 분야 목록에서 분야를 선택해 주세요.")
                category = names[category]
            row = db.execute(
                (
                    "SELECT t.merchant,i.digest,a.category,COALESCE(a.actual_merchant,'') AS "
                    "actual_merchant,COALESCE(a.memo,'') AS memo FROM transactions t LEFT JOIN "
                    "transaction_annotations a ON a.transaction_id=t.id JOIN imports i ON i.id=t.import_id WHERE t.id=? AND i.owner_id=?"
                ),
                (transaction_id, current_user_id()),
            ).fetchone()
            if not row:
                return fail("사용 내역을 찾지 못했습니다.", 404)
            # 저장을 시작하기 전에 검사해 일부만 저장되는 일이 없게 합니다.
            if not_spending is not None and row["digest"].startswith(bank_expenses.SOURCE_PREFIX):
                return fail("계좌 지출은 계좌 입출금 화면에서 소비 여부를 정해 주세요.")
            actual = payload.get("actual_merchant", row["actual_merchant"])
            memo = payload.get("memo", row["memo"])
            if (
                not isinstance(actual, str)
                or len(actual) > 500
                or not isinstance(memo, str)
                or len(memo) > 500
            ):
                return fail("실제 가맹점과 메모는 각각 500자 이하로 입력해 주세요.")
            actual = actual.strip()
            memo = memo.strip()
            service = payment_service(row["merchant"]) or payment_service(actual)
            scope = payload.get(
                "scope", "transaction" if service or row["category"] is not None else "merchant"
            )
            if scope not in ("merchant", "transaction"):
                return fail("분류 적용 범위를 선택해 주세요.")
            if update and scope == "merchant" and payment_service(actual or row["merchant"]):
                return fail(
                    "간편결제 이름에는 공통 분류를 적용할 수 없습니다. 실제 가맹점을 입력하거나 이번 거래만 선택해 주세요."
                )
            row_category = (
                (category if scope == "transaction" else None) if update else row["category"]
            )
            if update and scope == "merchant":
                key = merchant_key(actual or row["merchant"])
                if category is None:
                    db.execute(
                        "DELETE FROM merchant_categories WHERE merchant_key=? AND owner_id=?",
                        (key, current_user_id()),
                    )
                else:
                    db.execute(
                        (
                            "INSERT INTO merchant_categories(merchant_key,category,owner_id) VALUES(?,?,?) ON CONFLICT(owner_id,merchant_key) DO UPDATE SET "
                            "category=excluded.category"
                        ),
                        (key, category, current_user_id()),
                    )
            if row_category is None and not actual and not memo:
                db.execute(
                    "DELETE FROM transaction_annotations WHERE transaction_id=?", (transaction_id,)
                )
            else:
                db.execute(
                    (
                        "INSERT INTO transaction_annotations VALUES(?,?,?,?) ON CONFLICT(transaction_id) DO "
                        "UPDATE SET "
                        "category=excluded.category,actual_merchant=excluded.actual_merchant,memo=excluded.memo"
                    ),
                    (transaction_id, row_category, actual, memo),
                )
            if update:
                db.execute(
                    "INSERT OR IGNORE INTO category_reviews(transaction_id) VALUES(?)",
                    (transaction_id,),
                )
            if not_spending is not None:
                if not_spending:
                    # 이미 중복으로 제외한 거래는 그 사유를 유지합니다.
                    db.execute(
                        "INSERT OR IGNORE INTO transaction_exclusions(transaction_id,reason) "
                        "VALUES(?,'not_spending')",
                        (transaction_id,),
                    )
                else:
                    db.execute(
                        "DELETE FROM transaction_exclusions WHERE transaction_id=? "
                        "AND reason='not_spending'",
                        (transaction_id,),
                    )
            if shared_hidden is not None:
                db.execute(
                    (
                        "INSERT OR IGNORE INTO shared_hidden(transaction_id) VALUES(?)"
                        if shared_hidden
                        else "DELETE FROM shared_hidden WHERE transaction_id=?"
                    ),
                    (transaction_id,),
                )
        return jsonify(ok=True, scope=scope)
    finally:
        db.close()


@app.get("/api/categories")
def category_settings_data():
    """인증된 본인의 소비 분야 목록만 읽습니다."""
    db = connect()
    try:
        return jsonify(category_report(db, current_user_id()))
    finally:
        db.close()


def change_category_settings(action, key=None):
    """공통 개인 버전·세션 검사 안에서 분야 설정과 감사 이력을 원자적으로 변경합니다."""
    body = request.get_json(silent=True)
    if action != "delete" and (not isinstance(body, dict) or "name" not in body):
        return fail("분야 이름을 입력해 주세요.")
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if action == "delete":
                delete_category(db, current_user_id(), key)
            else:
                save_category(db, current_user_id(), body["name"], key)
            g.audit_target = "본인 소비 분야 설정"
            return jsonify(category_report(db, current_user_id()))
    except LookupError as error:
        return fail(str(error), 404)
    except ValueError as error:
        return fail(str(error))
    finally:
        db.close()


@app.post("/api/categories")
def category_settings_create():
    """본인만 사용하는 소비 분야를 추가합니다."""
    return change_category_settings("create")


@app.put("/api/categories/<path:key>")
def category_settings_update(key):
    """기존 분류 키를 유지하며 본인의 표시 이름만 바꿉니다."""
    return change_category_settings("update", key)


@app.delete("/api/categories/<path:key>")
def category_settings_delete(key):
    """본인에게 속한 미사용 추가 분야만 삭제합니다."""
    return change_category_settings("delete", key)


@app.get("/api/category-reviews")
def category_reviews_data():
    """대시보드가 본인 신규 내역의 확인 필요 건수만 가볍게 갱신하도록 제공합니다."""
    db = connect()
    try:
        return jsonify(category_review_summary(db, current_user_id()))
    finally:
        db.close()


@app.put("/api/category-reviews")
def category_reviews_update():
    """화면에 표시하고 사용자가 확인한 ID만 처리합니다. 이후 도착한 거래는 확인하지 않습니다."""
    body = request.get_json(silent=True)
    ids = body.get("ids") if isinstance(body, dict) else None
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 500
        or any(type(value) is not int or value <= 0 for value in ids)
    ):
        return fail("확인한 사용 내역을 1~500건 선택해 주세요.")
    ids = list(set(ids))
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in ids)
            owned = db.execute(
                "SELECT t.id FROM transactions t JOIN imports i ON i.id=t.import_id "
                "LEFT JOIN transaction_exclusions e ON e.transaction_id=t.id "
                f"WHERE i.owner_id=? AND e.transaction_id IS NULL AND t.id IN ({placeholders})",
                (current_user_id(), *ids),
            ).fetchall()
            if len(owned) != len(ids):
                return fail("확인할 사용 내역을 찾지 못했습니다.", 404)
            db.executemany(
                "INSERT OR IGNORE INTO category_reviews(transaction_id) VALUES(?)",
                [(value,) for value in ids],
            )
            g.audit_target = "본인 소비 분야 확인"
            return jsonify(category_review_summary(db, current_user_id()))
    finally:
        db.close()


# 8. 할부·중복·고정 지출 관리
# ─────────────────────────────────────────────────────────────────
@app.get("/api/finance")
def finance_data():
    """조회 월에 맞는 할부·중복·고정 지출 분석을 반환합니다."""
    db = connect()
    try:
        data = finance.insights(db, request.args.get("month", ""))
        data["cards"] = finance.card_catalog(db, CARDS)
        return jsonify(data)
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.route("/api/installments", methods=["POST"])
@app.route("/api/installments/<int:plan_id>", methods=["PUT", "DELETE"])
def installments(plan_id=None):
    """직접 등록한 할부 계획을 추가·수정·삭제합니다. 사용 내역 자체는 바꾸지 않습니다."""
    db = connect()
    try:
        if request.method == "DELETE":
            with db:
                result = db.execute(
                    "DELETE FROM installment_plans WHERE id=? AND owner_id=?",
                    (plan_id, current_user_id()),
                )
            return (
                jsonify(ok=True) if result.rowcount else fail("할부 기록을 찾지 못했습니다.", 404)
            )
        payload = finance.validate_plan(
            request.get_json(silent=True), finance.card_names(db, CARDS)
        )
        fields = tuple(
            payload[key]
            for key in ("name", "card", "principal", "months", "first_month", "paid_count")
        )
        with db:
            if plan_id is None:
                cursor = db.execute(
                    "INSERT INTO installment_plans(name,card,principal,months,first_month,paid_count,owner_id) VALUES(?,?,?,?,?,?,?)",
                    (*fields, current_user_id()),
                )
                plan_id = cursor.lastrowid
            else:
                result = db.execute(
                    "UPDATE installment_plans SET name=?,card=?,principal=?,months=?,first_month=?,paid_count=? WHERE id=? AND owner_id=?",
                    (*fields, plan_id, current_user_id()),
                )
                if not result.rowcount:
                    return fail("할부 기록을 찾지 못했습니다.", 404)
        return jsonify(id=plan_id)
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.put("/api/transactions/<int:transaction_id>/exclusion")
def transaction_exclusion(transaction_id):
    """다른 파일에 일치하는 거래가 있는지 확인한 뒤 집계 제외 또는 복원을 기록합니다."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or type(payload.get("excluded")) is not bool:
        return fail("제외 또는 복원 여부를 확인해 주세요.")
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                (
                    "SELECT t.*,i.card,e.transaction_id AS excluded FROM transactions t JOIN imports i ON"
                    " i.id=t.import_id LEFT JOIN transaction_exclusions e ON e.transaction_id=t.id WHERE "
                    "t.id=? AND i.owner_id=?"
                ),
                (transaction_id, current_user_id()),
            ).fetchone()
            if not row:
                return fail("사용 내역을 찾지 못했습니다.", 404)
            if payload["excluded"]:
                if row["excluded"] is None and not finance.can_exclude(db, row):
                    return fail(
                        "다른 파일에 일치하는 집계 대상이 없습니다. 중복 후보를 새로 확인해 주세요.",
                        409,
                    )
                db.execute(
                    "INSERT OR IGNORE INTO transaction_exclusions(transaction_id) VALUES(?)",
                    (transaction_id,),
                )
            else:
                if not bank_expenses.can_restore(db, transaction_id, current_user_id()):
                    return fail(
                        "계좌 입출금에서 실제 지출 여부와 카드 중복을 먼저 확인해 주세요.", 409
                    )
                db.execute(
                    "DELETE FROM transaction_exclusions WHERE transaction_id=?", (transaction_id,)
                )
        return jsonify(ok=True)
    finally:
        db.close()


@app.put("/api/transactions/<int:transaction_id>/recurring")
def recurring_decision(transaction_id):
    """반복 결제 후보를 고정 지출로 확인하거나 후보에서 제외한 결정을 저장합니다."""
    payload = request.get_json(silent=True)
    if (
        not isinstance(payload, dict)
        or "status" not in payload
        or payload["status"] not in ("confirmed", "ignored", None)
    ):
        return fail("고정 지출 확인 상태를 선택해 주세요.")
    db = connect()
    try:
        row = db.execute(
            (
                "SELECT COALESCE(NULLIF(a.actual_merchant,''),t.merchant) AS merchant,i.card FROM "
                "transactions t JOIN imports i ON i.id=t.import_id LEFT JOIN transaction_annotations "
                "a ON a.transaction_id=t.id WHERE t.id=? AND i.owner_id=?"
            ),
            (transaction_id, current_user_id()),
        ).fetchone()
        if not row:
            return fail("사용 내역을 찾지 못했습니다.", 404)
        with db:
            key = (row["card"], merchant_key(row["merchant"]), current_user_id())
            if payload["status"] is None:
                db.execute(
                    "DELETE FROM recurring_decisions WHERE card=? AND merchant_key=? AND owner_id=?",
                    key,
                )
            else:
                db.execute(
                    (
                        "INSERT INTO recurring_decisions(card,merchant_key,owner_id,status) VALUES(?,?,?,?) ON CONFLICT(owner_id,card,merchant_key) DO "
                        "UPDATE SET status=excluded.status"
                    ),
                    (*key, payload["status"]),
                )
        return jsonify(ok=True)
    finally:
        db.close()


@app.delete("/api/imports/<int:import_id>")
def delete_import(import_id):
    """등록한 명세서와 그에 속한 거래를 삭제합니다. 삭제 권한과 충돌 검사는 공통 계층을 거칩니다."""
    db = connect()
    try:
        with db:
            result = db.execute(
                "DELETE FROM imports WHERE id=? AND owner_id=?", (import_id, current_user_id())
            )
        if not result.rowcount:
            return fail("등록 기록을 찾을 수 없습니다.", 404)
        return jsonify(ok=True)
    finally:
        db.close()


@app.get("/sample.csv")
def sample():
    """실제 개인정보가 없는 연습용 CSV를 다운로드합니다."""
    return app.response_class(
        "\ufeff이용일자,가맹점명,이용금액\n2026-10-03,예시 마트,42000\n2026-10-05,예시 카페,5500\n2026-10-06,예시 마트,-12000\n",
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=sample.csv"},
    )


# 9. 월별 카드 청구·납부 현황
# ─────────────────────────────────────────────────────────────────
@app.get("/api/billing")
def billing_data():
    """카드별 월간 청구액·납부 여부·명세서 미확인 현황을 반환합니다."""
    db = connect()
    try:
        return jsonify(finance.billing_report(db, request.args.get("month", ""), CARDS))
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.post("/api/billing/cards")
def billing_card_create():
    """본인 지출 관리에 카드사를 추가합니다. 명세서 판독·문자 지원 범위를 추가하는 기능은 아닙니다."""
    payload = request.get_json(silent=True)
    db = connect()
    try:
        if not isinstance(payload, dict):
            return fail("추가할 카드사 정보를 입력해 주세요.")
        card = finance.validate_card_name(payload.get("card"))
        entries = finance.card_catalog(db, CARDS)
        if any(merchant_key(row["card"]) == merchant_key(card) for row in entries):
            return fail(
                "이미 등록된 카드사입니다. 기존 카드의 표시 이름과 사용 여부를 수정해 주세요.", 409
            )
        if sum(row["custom"] for row in entries) >= 30:
            return fail("개인 추가 카드사는 30개까지 등록할 수 있습니다.")
        finance.save_card_setting(db, card, {**payload, "enabled": True})
        return jsonify(ok=True, card=card), 201
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.put("/api/billing/settings/<card>")
def billing_settings(card):
    """사용 카드사·개인 표시 이름·기본 결제일을 저장합니다."""
    db = connect()
    try:
        if card not in finance.card_names(db, CARDS):
            return fail("본인에게 등록된 카드사를 선택해 주세요.")
        finance.save_card_setting(db, card, request.get_json(silent=True))
        return jsonify(ok=True)
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.put("/api/billing/<month>/<card>")
def billing_update(month, card):
    """납부할 월의 청구액·확인 상태·출금 예정일·납부 여부를 검증해 저장합니다."""
    db = connect()
    try:
        if card not in finance.card_names(db, CARDS):
            return fail("본인에게 등록된 카드사를 선택해 주세요.")
        fields = finance.validate_bill(request.get_json(silent=True), month)
        with db:
            db.execute(
                (
                    "INSERT INTO monthly_bills(month,card,statement_status,amount,due_on,paid,owner_id) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id,month,card) DO UPDATE SET "
                    "statement_status=excluded.statement_status,amount=excluded.amount,due_on=excluded.due_on,paid=excluded.paid"
                ),
                (month, card, *fields, current_user_id()),
            )
        return jsonify(ok=True)
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


# 10. 카드 상품별 전월 실적
# ─────────────────────────────────────────────────────────────────
# 카드사의 월 실적은 한국 시간 1일~말일 기준입니다.
card_performance_zone = ZoneInfo("Asia/Seoul")


def performance_month(value):
    """조회 월(YYYY-MM)과 그 전달을 돌려줍니다. 비어 있으면 한국 시간 기준 이번 달입니다."""
    if not value:
        value = datetime.now(card_performance_zone).strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("조회 월은 YYYY-MM 형식으로 입력해 주세요.")
    year, month = map(int, value.split("-"))
    previous = f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"
    return value, previous


@app.get("/api/card-performance")
def card_performance_data():
    """본인 카드별 이번 달·지난달 인정 실적, 카드 미지정 거래, 설정 화면용 선택지를 반환합니다."""
    user_id = current_user_id()
    db = connect()
    try:
        month, previous = performance_month(request.args.get("month", ""))
        if not card_performance.is_enabled(db, user_id):
            # 기능을 끈 사용자는 계산하지 않고 켜짐 여부만 알려 줍니다.
            return jsonify(
                enabled=False,
                month=month,
                previous_month=previous,
                cards=[],
                unassigned=[],
                statements=[],
                issuers=[],
                categories=[],
                presets=[],
            )
        transactions, labels = classified_transactions(db, user_id)
        products = card_performance.load_products(db, user_id)
        report = card_performance.summarize(products, transactions, month, previous)
        # 카드 한 장짜리 명세서를 연결할 수 있도록, 카드가 여러 장인 카드사의 명세서 묶음만 보냅니다.
        issuers = [p["issuer"] for p in products]
        multi = {issuer for issuer in issuers if issuers.count(issuer) > 1}
        statements = [
            dict(row)
            for row in db.execute(
                f"SELECT i.id, i.card, i.filename, i.card_product_id, COUNT(t.id) AS count "
                f"FROM imports i LEFT JOIN transactions t ON t.import_id=i.id "
                f"WHERE i.owner_id=? AND i.digest NOT LIKE 'codef:%' "
                f"AND i.digest NOT LIKE '{bank_expenses.SOURCE_PREFIX}%' "
                f"AND i.digest NOT LIKE '{manual_entries.SOURCE_PREFIX}%' "
                f"GROUP BY i.id ORDER BY i.id DESC",
                (user_id,),
            )
            if row["card"] in multi
        ]
        return jsonify(
            **report,
            enabled=True,
            statements=statements,
            issuers=finance.card_names(db, CARDS),
            categories=[{"key": key, "name": name} for key, name in labels.items()],
            presets=card_performance.PRESETS,
        )
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.put("/api/card-performance/settings")
def card_performance_settings():
    """본인 화면에서 카드 실적 기능(지출 관리 탭·대시보드 요약)을 켜거나 끕니다."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        return fail("켜기·끄기 값을 확인해 주세요.")
    db = connect()
    try:
        card_performance.set_enabled(db, current_user_id(), payload["enabled"])
        g.audit_target = "카드 실적 " + ("켜기" if payload["enabled"] else "끄기")
        return jsonify(ok=True, enabled=payload["enabled"])
    finally:
        db.close()


def card_product_payload(db, user_id):
    """요청 본문을 검증해 카드 상품 저장 값으로 바꿉니다."""
    return card_performance.validate_product(
        request.get_json(silent=True),
        finance.card_names(db, CARDS),
        set(category_labels(db, user_id)),
    )


def card_product_values(product):
    return (
        product["issuer"],
        product["name"],
        json.dumps(product["last_fours"]),
        json.dumps(product["benefits"], ensure_ascii=False),
        json.dumps(product["excluded_categories"], ensure_ascii=False),
        product["note"],
    )


@app.post("/api/card-performance/products")
def card_product_create():
    """본인 카드 상품을 추가합니다."""
    user_id = current_user_id()
    db = connect()
    try:
        product = card_product_payload(db, user_id)
        if (
            db.execute(
                "SELECT COUNT(*) FROM card_products WHERE owner_id=?", (user_id,)
            ).fetchone()[0]
            >= 50
        ):
            return fail("카드는 50장까지 등록할 수 있습니다.")
        cursor = db.execute(
            "INSERT INTO card_products(issuer,name,last_fours,benefits,excluded_categories,note,"
            "owner_id) VALUES(?,?,?,?,?,?,?)",
            (*card_product_values(product), user_id),
        )
        g.audit_target = f"카드 {product['name']}"
        return jsonify(ok=True, id=cursor.lastrowid), 201
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.put("/api/card-performance/products/<int:product_id>")
def card_product_update(product_id):
    """본인 카드 상품의 이름·끝자리·실적 기준·제외 분야를 수정합니다."""
    user_id = current_user_id()
    db = connect()
    try:
        product = card_product_payload(db, user_id)
        updated = db.execute(
            "UPDATE card_products SET issuer=?,name=?,last_fours=?,benefits=?,"
            "excluded_categories=?,note=? WHERE id=? AND owner_id=?",
            (*card_product_values(product), product_id, user_id),
        ).rowcount
        if not updated:
            return fail("수정할 카드를 찾지 못했습니다.", 404)
        g.audit_target = f"카드 {product['name']}"
        return jsonify(ok=True)
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.delete("/api/card-performance/products/<int:product_id>")
def card_product_delete(product_id):
    """본인 카드 상품을 지웁니다. 거래는 지우지 않고, 이 카드에 연결한 명세서만 연결을 풉니다."""
    user_id = current_user_id()
    db = connect()
    try:
        deleted = db.execute(
            "DELETE FROM card_products WHERE id=? AND owner_id=?", (product_id, user_id)
        ).rowcount
        if not deleted:
            return fail("삭제할 카드를 찾지 못했습니다.", 404)
        db.execute(
            "UPDATE imports SET card_product_id=NULL WHERE owner_id=? AND card_product_id=?",
            (user_id, product_id),
        )
        g.audit_target = f"카드 #{product_id}"
        return jsonify(ok=True)
    finally:
        db.close()


@app.put("/api/card-performance/imports/<int:import_id>")
def card_product_assign(import_id):
    """카드 한 장짜리 명세서를 통째로 특정 카드에 연결하거나(product_id) 연결을 풉니다(null)."""
    user_id = current_user_id()
    payload = request.get_json(silent=True) or {}
    product_id = payload.get("product_id")
    db = connect()
    try:
        source = db.execute(
            "SELECT card FROM imports WHERE id=? AND owner_id=? AND digest NOT LIKE 'codef:%'",
            (import_id, user_id),
        ).fetchone()
        if not source:
            return fail("연결할 명세서를 찾지 못했습니다.", 404)
        if product_id is not None:
            if isinstance(product_id, bool) or not isinstance(product_id, int):
                return fail("연결할 카드를 선택해 주세요.")
            if not db.execute(
                "SELECT 1 FROM card_products WHERE id=? AND owner_id=? AND issuer=?",
                (product_id, user_id, source["card"]),
            ).fetchone():
                return fail("같은 카드사의 본인 카드만 연결할 수 있습니다.")
        db.execute(
            "UPDATE imports SET card_product_id=? WHERE id=? AND owner_id=?",
            (product_id, import_id, user_id),
        )
        g.audit_target = f"명세서 #{import_id}"
        return jsonify(ok=True)
    finally:
        db.close()


# 11. 화면 기능 켜기·끄기, 연말정산 카드 공제, 분야별 예산, 구독·정기 결제
# ─────────────────────────────────────────────────────────────────
def kst_today():
    return datetime.now(card_performance_zone).date()


@app.get("/api/features")
def features_data():
    """본인 화면 기능의 켜짐 여부를 한 번에 돌려줍니다."""
    db = connect()
    try:
        return jsonify(
            features=features.enabled_map(db, current_user_id()), names=features.FEATURES
        )
    finally:
        db.close()


@app.put("/api/features")
def features_update():
    """본인 화면 기능 하나를 켜거나 끕니다. 꺼도 입력한 설정은 지우지 않습니다."""
    payload = request.get_json(silent=True)
    if (
        not isinstance(payload, dict)
        or payload.get("feature") not in features.FEATURES
        or not isinstance(payload.get("enabled"), bool)
    ):
        return fail("켜거나 끌 기능을 확인해 주세요.")
    db = connect()
    try:
        features.set_enabled(db, current_user_id(), payload["feature"], payload["enabled"])
        g.audit_target = features.FEATURES[payload["feature"]] + (
            " 켜기" if payload["enabled"] else " 끄기"
        )
        return jsonify(ok=True)
    finally:
        db.close()


@app.get("/api/tax-deduction")
def tax_deduction_data():
    """올해(또는 지정 연도) 카드 사용액으로 예상 소득공제와 25% 문턱 진행 상황을 계산합니다."""
    user_id = current_user_id()
    db = connect()
    try:
        if not features.is_enabled(db, user_id, "tax_deduction"):
            return jsonify(enabled=False)
        today = kst_today()
        raw = request.args.get("year", "")
        if raw and not re.fullmatch(r"20[2-9][0-9]", raw):
            return fail("연도를 확인해 주세요.")
        year = int(raw) if raw else today.year
        transactions, _ = classified_transactions(db, user_id)
        settings = tax_deduction.load_settings(db, user_id, year)
        return jsonify(
            enabled=True,
            issuers=finance.card_names(db, CARDS),
            **tax_deduction.report(transactions, settings, year, today),
        )
    finally:
        db.close()


@app.put("/api/tax-deduction/settings")
def tax_deduction_settings():
    """총급여·자녀 수·카드사별 신용/체크 구분·모아 밖 사용액을 저장합니다."""
    user_id = current_user_id()
    db = connect()
    try:
        settings = tax_deduction.validate_settings(
            request.get_json(silent=True), finance.card_names(db, CARDS)
        )
        tax_deduction.save_settings(db, user_id, settings)
        g.audit_target = f"{settings['year']}년 연말정산 카드 공제 설정"
        return jsonify(ok=True)
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.get("/api/budgets")
def budgets_data():
    """조회 월(기본 이번 달)의 분야별 예산 대비 사용액과 상태를 돌려줍니다."""
    user_id = current_user_id()
    db = connect()
    try:
        if not features.is_enabled(db, user_id, "budgets"):
            return jsonify(enabled=False)
        month, _ = performance_month(request.args.get("month", ""))
        transactions, labels = classified_transactions(db, user_id)
        return jsonify(
            enabled=True,
            categories=[{"key": key, "name": name} for key, name in labels.items()],
            **budgets.report(transactions, budgets.load(db, user_id), labels, month, kst_today()),
        )
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.put("/api/budgets")
def budgets_update():
    """본인 분야별 월 예산을 입력한 목록으로 바꿉니다. 0원은 그 분야 예산을 지웁니다."""
    user_id = current_user_id()
    db = connect()
    try:
        items = budgets.validate(request.get_json(silent=True), set(category_labels(db, user_id)))
        budgets.save(db, user_id, items)
        g.audit_target = "분야별 월 예산"
        return jsonify(ok=True)
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.get("/api/subscriptions")
def subscriptions_data():
    """반복 결제 후보를 월·연 비용, 가격 인상, 7일 안에 다가오는 결제로 정리합니다."""
    user_id = current_user_id()
    db = connect()
    try:
        if not features.is_enabled(db, user_id, "subscriptions"):
            return jsonify(enabled=False)
        today = kst_today()
        recurring = finance.insights(db, today.strftime("%Y-%m"))["recurring"]
        transactions, labels = classified_transactions(db, user_id)
        categories = {row["id"]: row["category_key"] for row in transactions}
        return jsonify(enabled=True, **subscriptions.report(recurring, categories, labels, today))
    finally:
        db.close()


# 12. 직접 입력 지출(현금·계좌이체 등)
# ─────────────────────────────────────────────────────────────────
def manual_entry_payload(db, user_id):
    return manual_entries.validate(request.get_json(silent=True), category_catalog(db, user_id))


@app.put("/api/transactions/<int:transaction_id>/foreign-amount")
def foreign_amount_confirm(transaction_id):
    """해외 결제의 적용 환율 또는 원화 결제액을 받아 거래 금액을 원화로 정합니다(foreign_payments.py)."""
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            amount = foreign_payments.confirm(
                db, current_user_id(), transaction_id, request.get_json(silent=True)
            )
        g.audit_target = f"해외 결제 원화 확정 #{transaction_id}"
        return jsonify(amount=amount)
    except ValueError as exc:
        return fail(str(exc))
    except LookupError as exc:
        return fail(str(exc), 404)
    finally:
        db.close()


@app.post("/api/transactions/manual")
def manual_entry_create():
    """명세서로 들어오지 않는 지출을 직접 적습니다. 분야는 사용자가 고른 것으로 확인 완료 처리합니다."""
    user_id = current_user_id()
    db = connect()
    try:
        with db:
            transaction_id = manual_entries.create(db, user_id, manual_entry_payload(db, user_id))
        g.audit_target = f"직접 입력 #{transaction_id}"
        return jsonify(id=transaction_id), 201
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.put("/api/transactions/manual/<int:transaction_id>")
def manual_entry_update(transaction_id):
    """직접 입력한 지출의 날짜·사용처·금액·결제 수단·분야·메모를 고칩니다."""
    user_id = current_user_id()
    db = connect()
    try:
        with db:
            manual_entries.update(db, user_id, transaction_id, manual_entry_payload(db, user_id))
        g.audit_target = f"직접 입력 #{transaction_id}"
        return jsonify(ok=True)
    except ValueError as exc:
        return fail(str(exc))
    except LookupError as exc:
        return fail(str(exc), 404)
    finally:
        db.close()


@app.delete("/api/transactions/manual/<int:transaction_id>")
def manual_entry_delete(transaction_id):
    """직접 입력한 지출 한 건을 지웁니다. 카드·계좌 거래는 이 API로 지울 수 없습니다."""
    user_id = current_user_id()
    db = connect()
    try:
        with db:
            manual_entries.delete(db, user_id, transaction_id)
        g.audit_target = f"직접 입력 #{transaction_id}"
        return jsonify(ok=True)
    except LookupError as exc:
        return fail(str(exc), 404)
    finally:
        db.close()


# 영수증 사진(receipts.py): 본인 거래에만 붙이고 본인만 봅니다.
@app.post("/api/transactions/<int:transaction_id>/receipts")
def receipt_upload(transaction_id):
    """거래 한 건에 영수증 사진 한 장을 붙입니다. 화면이 줄인 JPEG를 보내지만 서버도 형식·크기를 확인합니다."""
    upload = request.files.get("file")
    if not upload:
        return fail("사진 파일을 선택해 주세요.")
    data = upload.read(receipts.MAX_BYTES + 1)
    db = connect()
    try:
        with db:
            receipt_id = receipts.add(db, current_user_id(), transaction_id, data)
        g.audit_target = f"거래 #{transaction_id}"
        return jsonify(id=receipt_id), 201
    except ValueError as exc:
        return fail(str(exc))
    except LookupError as exc:
        return fail(str(exc), 404)
    finally:
        db.close()


@app.get("/api/receipts/<int:receipt_id>")
def receipt_image(receipt_id):
    """본인 거래에 붙은 사진만 저장할 때 확인한 이미지 형식으로 돌려줍니다."""
    db = connect()
    try:
        row = receipts.find(db, current_user_id(), receipt_id)
        if row is None:
            return fail("사진을 찾지 못했습니다.", 404)
        response = app.response_class(bytes(row["data"]), mimetype=row["mime"])
        response.headers["Content-Disposition"] = "inline"
        return response
    finally:
        db.close()


@app.get("/api/shared-receipts/<int:receipt_id>")
def shared_receipt_image(receipt_id):
    """함께 보기에 지금 보이는 거래의 영수증 사진을 읽기 전용으로 돌려줍니다(shared_spending.py)."""
    db = connect()
    try:
        row = shared_spending.shared_receipt(db, current_user_id(), receipt_id)
        if row is None:
            return fail("사진을 찾지 못했습니다.", 404)
        response = app.response_class(bytes(row["data"]), mimetype=row["mime"])
        response.headers["Content-Disposition"] = "inline"
        return response
    finally:
        db.close()


@app.delete("/api/receipts/<int:receipt_id>")
def receipt_delete(receipt_id):
    """사진 한 장을 지웁니다."""
    db = connect()
    try:
        with db:
            transaction_id = receipts.delete(db, current_user_id(), receipt_id)
        g.audit_target = f"거래 #{transaction_id}"
        return jsonify(ok=True)
    except LookupError as exc:
        return fail(str(exc), 404)
    finally:
        db.close()


# 13. 사용 내역 엑셀 내려받기
# ─────────────────────────────────────────────────────────────────
@app.get("/api/export.xlsx")
def export_transactions():
    """사용 내역 화면의 조건 그대로 본인 거래를 엑셀 파일로 내려줍니다. 서버에 파일을 남기지 않습니다."""
    from urllib.parse import quote

    user_id = current_user_id()
    try:
        cond = export_xlsx.parse(request.args)
    except ValueError as exc:
        return fail(str(exc))
    db = connect()
    try:
        transactions, _ = classified_transactions(db, user_id)
    finally:
        db.close()
    rows = export_xlsx.select(transactions, cond)
    name = f"모아-사용내역-{export_xlsx.period_label(cond)}.xlsx"
    g.audit_target = f"사용 내역 엑셀 {len(rows)}건"
    return app.response_class(
        export_xlsx.workbook(rows, cond),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=moa-export.xlsx; "
            f"filename*=UTF-8''{quote(name)}",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/household")
def household_data():
    """개인 상세를 포함하지 않는 통합 지출 합계만 반환합니다."""
    db = connect()
    try:
        return jsonify(personal_data.aggregate_spending(db, request.args.get("month", "")))
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.get("/api/shared-management")
def shared_management_data():
    """허용된 관리 정보만 구성원별로 함께 열람합니다. 수정 API는 제공하지 않습니다."""
    from shared_management import report

    db = connect()
    try:
        return jsonify(report(db, request.args.get("month", ""), CARDS))
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


@app.get("/api/shared-transactions")
def shared_transactions_data():
    """함께 보기를 켠 다른 구성원의 한 달 사용 내역을 읽기 전용으로 반환합니다."""
    from shared_spending import report

    db = connect()
    try:
        return jsonify(report(db, current_user_id(), request.args.get("month", "")))
    except ValueError as exc:
        return fail(str(exc))
    finally:
        db.close()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
