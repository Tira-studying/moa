"""암호가 있는 HTML 명세서를 별도 Chromium 프로세스에서 한 번 읽습니다.

원본과 비밀번호는 표준 입력으로 전달하고, 결과 표만 JSON으로 돌려받습니다.
카드사 웹페이지 실행은 scripts/unlock.cjs가 담당하며 시간·크기·동시 처리 수를 제한합니다.
"""

import json
import os
import re
import unicodedata
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from diagnostic_structure import sanitize_structure


class StatementReadError(ValueError):
    """화면용 오류 메시지와 개인정보를 가상화한 구조 진단을 함께 전달합니다."""

    def __init__(self, message, structure=None):
        super().__init__(message)
        self.structure = structure


BUSY = threading.BoundedSemaphore(1)
MESSAGES = {
    "browser": "보안 브라우저를 시작하지 못했습니다. Chromium 설치와 샌드박스 실행 권한을 확인해 주세요.",
    "unsupported": "선택한 카드사에 맞는 보안 HTML 입력 화면을 찾지 못했습니다.",
    "unlock": "명세서가 열리지 않았습니다. 비밀번호 또는 파일의 열람 조건을 확인해 주세요.",
    "online_failed": "삼성카드 공식 페이지에서 거래 내역을 불러오지 못했습니다. NAS 인터넷 연결이나 명세서 열람 기간을 확인해 주세요.",
    "online_required": "암호 해제 후 외부 웹페이지를 여는 안내가 발견됐습니다. 현재 오프라인 읽기 방식으로는 해당 페이지의 거래 내역을 가져올 수 없습니다. 삼성카드 공식 사이트 연결 옵션을 선택하고 다시 열어 주세요.",
    "no_table": "명세서 해제 완료를 확인하지 못했습니다. 비밀번호 또는 명세서 처리 방식을 추가 확인해야 합니다.",
    "unlocked_no_table": "명세서 해제 완료 응답을 받았지만 거래 표를 찾지 못했습니다. 표 양식에 대한 추가 지원이 필요합니다.",
    "script_error": "명세서 처리 스크립트에서 오류가 발생했습니다. 비밀번호 오류로 단정할 수 없으며 처리 방식 확인이 필요합니다.",
    "incomplete": "더보기 확장 또는 명세서 소계 건수 확인을 완료하지 못했습니다. 일부 내역만 저장되는 것을 막기 위해 가져오기를 중단했습니다.",
    "size": "추출한 명세서가 처리 한도를 초과했습니다.",
    "processing": "명세서 처리에 실패했습니다. 이 파일 형식은 추가 확인이 필요합니다.",
}


def normalize_statement_tables(tables, card):
    """우리카드의 명확한 카드 구분·소계 행을 정리합니다. 불명확한 거래 후보는 버리지 않습니다."""
    if card != "우리":
        return tables
    compact = lambda value: re.sub(
        r"[\s\u200b\ufeff]+", "", unicodedata.normalize("NFKC", str("" if value is None else value))
    )
    date_label = re.compile("이용일|사용일|거래일|승인일|매출일|날짜|일자")
    merchant_label = re.compile("가맹점|이용처|사용처|상호|업체명")
    amount_label = re.compile("금액|원화|이용액|사용액|청구액")
    date_value = re.compile(
        r"^(?:\d{8}(?:$|\s)|\d{4}[-./년]\s*\d{1,2}[-./월]\s*\d{1,2}|\d{1,2}\s*[-./]\s*\d{1,2})"
    )
    summary = re.compile(
        r"^(?:소계|합계|총계|이용합계|카드별합계|이용금액합계)(?:$|[(：:])|^청구합계(?:$|[-(：:])"
    )
    card_label = re.compile(
        r"^\([A-Za-z]\d{2,8}\).*카드|^(?:사용카드|이용카드|카드번호)(?:$|[:：])"
    )
    result = []
    for table in tables:
        header = next(
            (
                i
                for i, row in enumerate(table[:10])
                if any(date_label.search(compact(c)) for c in row)
                and any(merchant_label.search(compact(c)) for c in row)
                and any(amount_label.search(compact(c)) for c in row)
            ),
            None,
        )
        if header is None:
            result.append(table)
            continue
        columns = [compact(c) for c in table[header]]
        date_index = next(i for i, c in enumerate(columns) if date_label.search(c))
        kept = []
        for i, row in enumerate(table):
            values = [compact(c) for c in row]
            value = values[date_index] if date_index < len(values) else ""
            if i == header or date_value.match(
                str(row[date_index] if date_index < len(row) else "").strip()
            ):
                kept.append(row)
                continue
            unique = {v for v in values if v}
            repeated_header = i > header and values == columns
            card_section = card_label.search(value) and (
                len(unique) == 1 or value in ("사용카드", "이용카드", "카드번호")
            )
            if not unique or repeated_header or summary.search(value) or card_section:
                continue
            # Unknown rows remain visible and fail validation instead of losing transactions.
            kept.append(row)
        result.append(kept)
    return result


def unlock_statement(data, password, *, probe=False, card="삼성", allow_online=False):
    """격리된 브라우저 판독기를 실행하고 시간·크기·완전성 검사 후 추출 표를 반환합니다."""
    if card not in ("삼성", "롯데", "현대", "우리"):
        raise ValueError("현재 보안 HTML은 삼성·롯데·현대·우리카드를 지원합니다.")
    if allow_online and card != "삼성":
        raise ValueError("공식 사이트 연결은 삼성카드만 지원합니다.")
    try:
        html = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            html = data.decode("cp949")
        except UnicodeDecodeError:
            raise ValueError("HTML 파일의 인코딩을 읽을 수 없습니다.") from None
    if not password and not probe:
        raise ValueError("명세서 비밀번호를 입력해 주세요.")
    if len(password) > 128:
        raise ValueError("비밀번호는 128자 이하로 입력해 주세요.")
    if not BUSY.acquire(blocking=False):
        raise ValueError("다른 명세서를 처리 중입니다. 잠시 후 다시 시도해 주세요.")
    process = None
    try:
        root = Path(__file__).parent
        environment = os.environ.copy()
        if not environment.get("MOA_CHROMIUM") and shutil.which("chromium"):
            environment["MOA_CHROMIUM"] = shutil.which("chromium")
        process = subprocess.Popen(
            ["node", str(root / "scripts" / "unlock.cjs")],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=environment,
        )
        encoded = json.dumps(
            dict(
                html=html, password=password, probe=probe, card=card, allowOnline=bool(allow_online)
            )
        ).encode()
        try:
            output, _ = process.communicate(encoded, timeout=55 if allow_online else 30)
        except subprocess.TimeoutExpired:
            raise ValueError(
                "처리 시간이 초과되었습니다. 비밀번호와 파일을 확인해 주세요."
            ) from None
        finally:
            encoded = b""
            password = ""
        try:
            result = json.loads(output)
        except (ValueError, UnicodeDecodeError):
            raise ValueError(MESSAGES["processing"]) from None
        if result.get("error"):
            message = MESSAGES.get(result["error"], MESSAGES["processing"])
            if result["error"] == "online_required" and card != "삼성":
                message = "암호 해제 후 외부 웹페이지를 여는 안내가 발견됐습니다. 현재 이 카드사의 외부 페이지 연결은 지원하지 않습니다. 원본 명세서를 직접 열어 확인해 주세요."
            diagnostics = result.get("diagnostics")
            if isinstance(diagnostics, dict):
                # Only bounded counts leave the worker; never errors, HTML or values.
                parts = []
                for key, label in [
                    ("expansionReason", "확장 중단 사유"),
                    ("httpErrors", "서버 오류 응답"),
                    ("lastHttpStatus", "마지막 응답 상태"),
                    ("onlineActive", "응답 대기"),
                    ("onlineRequests", "공식 요청"),
                    ("onlineFailures", "공식 연결 실패"),
                    ("popupPages", "추가 창"),
                    ("writtenNavigationScripts", "이동 스크립트"),
                    ("writtenScripts", "결과 스크립트"),
                    ("writtenForms", "결과 폼"),
                    ("writtenLinks", "결과 링크"),
                    ("writtenFrames", "결과 프레임"),
                    ("externalDestinations", "외부 이동 대상"),
                    ("samsungDestinations", "삼성 이동 대상"),
                    ("readerRevision", "읽기 버전"),
                    ("unlockStarted", "해제 시작"),
                    ("unlockEnded", "해제 완료"),
                    ("writeCalls", "화면 쓰기"),
                    ("writtenChars", "화면 문자 수"),
                    ("passwordFields", "암호 입력란"),
                    ("cspBlocks", "보안 차단"),
                    ("frames", "프레임"),
                    ("tables", "표"),
                    ("scriptErrors", "스크립트 오류"),
                    ("blockedRequests", "차단 요청"),
                    ("listDateNodes", "목록 날짜 후보"),
                    ("expectedRecords", "명세서 건수"),
                    ("readRecords", "인식 건수"),
                    ("moreClicks", "더보기 클릭"),
                ]:
                    value = diagnostics.get(key)
                    if isinstance(value, int) and 0 <= value <= (
                        2 * 1024 * 1024 if key == "writtenChars" else 100000
                    ):
                        parts.append(f"{label} {value}")
                message += f" [진단: {result['error']}; {', '.join(parts)}]"
            structure = sanitize_structure(result.pop("rawStructure", None))
            raise StatementReadError(message, structure)
        if probe and result.get("probe"):
            return []
        tables = result.get("tables", [])
        if not tables:
            raise ValueError(MESSAGES["no_table"])
        return tables
    except OSError:
        raise ValueError(MESSAGES["browser"]) from None
    finally:
        # Also reap descendants if a renderer hangs or the worker exits early.
        if process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        BUSY.release()
