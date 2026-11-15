"""사용 내역 엑셀(xlsx) 내보내기: 사용 내역 화면에 보이는 조건 그대로 거래를 골라 파일로 만듭니다.

조건은 화면(transactions.js의 renderTransactions)과 같습니다.
  - period: all(전체) · month(month=YYYY-MM) · custom(start·end=YYYY-MM-DD) · day(start=YYYY-MM-DD, 달력에서 고른 날)
  - visibility: active(집계 대상) · excluded(제외한 내역) · all(전체 기록)
  - card, category(분야 이름), q(가맹점·실제 가맹점·메모 검색), review_only=1(분야 확인 필요만)
시트: '사용 내역'(거래 한 줄씩)과 '분야별 합계'.
파일은 서버에 저장하지 않고 응답으로만 보냅니다(app.py /api/export.xlsx).
"""

import io
import re
from collections import defaultdict
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

PERIODS = ("all", "month", "custom", "day")
VISIBILITY = ("active", "excluded", "all")
KIND_NAMES = {"card": "카드", "bank": "계좌", "manual": "직접 입력"}
# 엑셀이 수식으로 읽을 수 있는 첫 글자입니다. 가맹점·메모가 이렇게 시작하면 글자로 고정합니다.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
HEADER_FILL = PatternFill("solid", fgColor="EAF4F0")


def valid_day(value):
    try:
        return isinstance(value, str) and len(value) == 10 and bool(date.fromisoformat(value))
    except ValueError:
        return False


# 1. 조건 검사와 거래 고르기
# ─────────────────────────────────────────────────────────────────
def parse(args):
    """요청 주소의 조건을 검사합니다. 잘못된 값은 ValueError로 알립니다."""
    period = args.get("period", "all")
    if period not in PERIODS:
        raise ValueError("조회 기간을 확인해 주세요.")
    month, start, end = args.get("month", ""), args.get("start", ""), args.get("end", "")
    if period == "month" and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("조회 월을 확인해 주세요.")
    if period == "day":
        if not valid_day(start):
            raise ValueError("날짜를 확인해 주세요.")
        end = start
    if period == "custom":
        if (start and not valid_day(start)) or (end and not valid_day(end)):
            raise ValueError("시작일·종료일을 확인해 주세요.")
        if start and end and start > end:
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
    visibility = args.get("visibility", "active")
    if visibility not in VISIBILITY:
        raise ValueError("집계 포함 여부를 확인해 주세요.")
    texts = {name: args.get(name, "") for name in ("card", "category", "q")}
    if any(len(value) > 200 for value in texts.values()):
        raise ValueError("검색 조건이 너무 깁니다.")
    return {
        "period": period,
        "month": month,
        "start": start,
        "end": end,
        "visibility": visibility,
        "review_only": args.get("review_only") == "1",
        **texts,
        "q": texts["q"].strip().lower(),
    }


def select(rows, cond):
    """화면 목록과 같은 조건·순서(최근 날짜 먼저)로 거래를 고릅니다."""

    def keep(row):
        if cond["visibility"] == "active" and row["excluded"]:
            return False
        if cond["visibility"] == "excluded" and not row["excluded"]:
            return False
        used = row["used_on"]
        if cond["period"] == "month" and not used.startswith(cond["month"]):
            return False
        if cond["period"] in ("custom", "day") and (
            (cond["start"] and used < cond["start"]) or (cond["end"] and used > cond["end"])
        ):
            return False
        if cond["review_only"] and row["category_reviewed"] is not False:
            return False
        if cond["card"] and row["card"] != cond["card"]:
            return False
        if cond["category"] and (row["category"] or "미분류") != cond["category"]:
            return False
        text = " ".join([row["merchant"], row["actual_merchant"] or "", row["memo"] or ""])
        return cond["q"] in text.lower()

    return sorted(
        (row for row in rows if keep(row)),
        key=lambda row: (row["used_on"], row["id"]),
        reverse=True,
    )


def period_label(cond):
    if cond["period"] == "month":
        return cond["month"]
    if cond["period"] == "day":
        return cond["start"]
    if cond["period"] == "custom":
        return f"{cond['start'] or '처음'}~{cond['end'] or '최근'}"
    return "전체기간"


# 2. 엑셀 파일 만들기
# ─────────────────────────────────────────────────────────────────
def safe(value):
    """글자 칸이 수식으로 실행되지 않게 합니다(CSV·엑셀 수식 주입 방지)."""
    value = value or ""
    return "'" + value if value.startswith(FORMULA_START) else value


def write_sheet(sheet, header, rows, widths, money_columns=()):
    sheet.append(header)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")
    for row in rows:
        sheet.append(row)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for column in money_columns:
        for (cell,) in sheet.iter_rows(min_row=2, min_col=column, max_col=column):
            cell.number_format = "#,##0"
    sheet.freeze_panes = "A2"


def workbook(rows, cond):
    """선택한 거래로 xlsx 파일 내용(bytes)을 만듭니다."""
    book = Workbook()
    detail = book.active
    detail.title = "사용 내역"
    write_sheet(
        detail,
        ["이용일", "사용처", "원본 가맹점", "분야", "카드·계좌", "구분", "금액", "메모", "집계"],
        [
            [
                row["used_on"],
                safe(row["actual_merchant"] or row["merchant"]),
                safe(row["merchant"]),
                row["category"] or "미분류",
                row["card"],
                KIND_NAMES.get(row["payment_kind"], row["payment_kind"]),
                row["amount"],
                safe(row["memo"]),
                "제외" if row["excluded"] else "포함",
            ]
            for row in rows
        ],
        [12, 26, 26, 12, 14, 10, 13, 30, 7],
        money_columns=(7,),
    )
    total = sum(row["amount"] for row in rows)
    detail.append([])
    detail.append(["합계", f"{len(rows)}건", None, None, None, None, total])
    detail.cell(detail.max_row, 1).font = Font(bold=True)
    detail.cell(detail.max_row, 7).number_format = "#,##0"
    detail.cell(detail.max_row, 7).font = Font(bold=True)

    sums = defaultdict(lambda: [0, 0])
    for row in rows:
        sums[row["category"] or "미분류"][0] += row["amount"]
        sums[row["category"] or "미분류"][1] += 1
    summary = book.create_sheet("분야별 합계")
    ordered = sorted(sums.items(), key=lambda item: -item[1][0])
    write_sheet(
        summary,
        ["분야", "금액", "건수", "비중"],
        [
            [name, amount, count, round(amount / total, 4) if total > 0 else None]
            for name, (amount, count) in ordered
        ],
        [16, 14, 8, 9],
        money_columns=(2,),
    )
    for (cell,) in summary.iter_rows(min_row=2, min_col=4, max_col=4):
        cell.number_format = "0.0%"
    summary.append(["합계", total, len(rows), None])
    summary.cell(summary.max_row, 2).number_format = "#,##0"
    for cell in summary[summary.max_row]:
        cell.font = Font(bold=True)
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()
