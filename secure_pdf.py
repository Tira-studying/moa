"""신한 PDF를 별도 Python 프로세스에서 제한된 시간 동안 읽는 연결 계층입니다.

원본과 비밀번호는 메모리와 표준 입력으로만 전달합니다. 실제 표 추출은
scripts/read_pdf.py가 담당하고, 여기서는 시간 초과·오류·프로세스 정리를 처리합니다.
"""

import base64
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from secure_html import BUSY

MESSAGES = {
    "password": "PDF 비밀번호가 필요하거나 입력한 비밀번호가 다릅니다. 모아에서 다시 입력해 주세요.",
    "format": "유효한 PDF 파일을 선택해 주세요.",
    "pages": "PDF는 30쪽 이하만 처리할 수 있습니다.",
    "size": "PDF 추출 결과가 처리 한도를 초과했습니다.",
    "image": "텍스트가 없는 이미지 PDF입니다. 현재 OCR은 지원하지 않습니다.",
    "no_table": "PDF는 열렸지만 거래 표를 찾지 못했습니다. 신한카드 양식에 대한 추가 지원이 필요합니다.",
    "processing": "PDF를 처리하지 못했습니다. 손상된 파일이거나 추가 지원이 필요한 형식일 수 있습니다.",
}


def unlock_pdf(data, password=""):
    """별도 PDF 판독 프로세스를 실행하고 제한 시간 안에 받은 표를 반환한 뒤 프로세스를 정리합니다."""
    if len(password) > 128:
        raise ValueError("비밀번호는 128자 이하로 입력해 주세요.")
    if not BUSY.acquire(blocking=False):
        raise ValueError("다른 명세서를 처리 중입니다. 잠시 후 다시 시도해 주세요.")
    process = None
    try:
        root = Path(__file__).parent
        process = subprocess.Popen(
            [sys.executable, str(root / "scripts" / "read_pdf.py")],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        payload = json.dumps(
            {"data": base64.b64encode(data).decode(), "password": password}
        ).encode()
        password = ""
        try:
            output, _ = process.communicate(payload, timeout=25)
        except subprocess.TimeoutExpired:
            raise ValueError("PDF 처리 시간이 초과되었습니다. 파일을 확인해 주세요.") from None
        finally:
            payload = b""
        try:
            result = json.loads(output)
        except (ValueError, UnicodeDecodeError):
            raise ValueError(MESSAGES["processing"]) from None
        if result.get("error"):
            raise ValueError(MESSAGES.get(result["error"], MESSAGES["processing"]))
        if not result.get("tables"):
            raise ValueError(MESSAGES["no_table"])
        return result["tables"]
    except OSError:
        raise ValueError(MESSAGES["processing"]) from None
    finally:
        if process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        BUSY.release()
