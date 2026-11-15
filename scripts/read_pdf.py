"""별도 프로세스에서 실행되는 PDF 표 추출기입니다.

표준 입력의 JSON에서 PDF와 암호를 읽고, 텍스트 표 또는 정해진 오류 코드만 출력합니다.
PDF 쪽수와 추출 크기를 제한하며 이미지 PDF의 OCR은 수행하지 않습니다.
"""

import base64
import io
import json
import logging
import re
import sys

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

logging.disable(logging.CRITICAL)
DATE = re.compile(
    r"^(?:\d{4}[-./년]\s*\d{1,2}[-./월]\s*\d{1,2}|\d{2}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[-./]\d{1,2}|\d{8})(?:일|\s|$)"
)


def candidates(rows):
    """추출한 표에 날짜 후보와 최소한의 거래 열이 있는지 확인하고 셀 공백을 정리합니다."""
    if len(rows) > 20000 or any(len(row) > 100 for row in rows):
        raise OverflowError()
    clean = [[re.sub(r"\s+", " ", str(cell or "")).strip() for cell in row] for row in rows]
    clean = [row for row in clean if any(row)]
    dates = sum(any(DATE.match(cell) for cell in row) for row in clean)
    return clean if dates and any(len(row) >= 3 for row in clean) else None


def read_pdf(data, password):
    """쪽별 텍스트 표를 찾고 결과 크기를 제한해 표 목록 또는 오류 코드를 반환합니다."""
    tables = []
    characters = 0
    with pdfplumber.open(io.BytesIO(data), password=password) as doc:
        if len(doc.pages) > 30:
            return {"error": "pages"}
        for page in doc.pages:
            characters += len(page.chars)
            ruled = page.extract_tables()
            found = [value for rows in ruled if (value := candidates(rows))]
            if not found:
                text_tables = page.extract_tables(
                    {"vertical_strategy": "text", "horizontal_strategy": "text"}
                )
                found = [value for rows in text_tables if (value := candidates(rows))]
            if not found:
                text = page.extract_text(layout=True) or ""
                rows = [
                    re.split(r"\s{2,}", line.strip()) for line in text.splitlines() if line.strip()
                ]
                value = candidates(rows)
                if value:
                    found = [value]
            tables.extend(found)
            if len(json.dumps(tables, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
                return {"error": "size"}
            page.close()
    if not tables:
        return {"error": "image" if not characters else "no_table"}
    return {"tables": tables}


def main():
    try:
        raw = sys.stdin.buffer.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            result = {"error": "size"}
        else:
            payload = json.loads(raw)
            data = base64.b64decode(payload["data"], validate=True)
            if not data.startswith(b"%PDF-"):
                result = {"error": "format"}
            else:
                result = read_pdf(data, payload.get("password", ""))
    except PDFPasswordIncorrect:
        result = {"error": "password"}
    except PdfminerException as error:
        result = {
            "error": (
                "password" if isinstance(error.__context__, PDFPasswordIncorrect) else "processing"
            )
        }
    except OverflowError:
        result = {"error": "size"}
    except Exception:
        result = {"error": "processing"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
