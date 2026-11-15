"""NAS 터미널에서 6개 카드사 실제 조회 권한과 응답 건수를 확인하는 일회성 도구입니다.

키는 기존 비밀 파일에서 읽습니다. 카드사 로그인 입력·추가 인증·조회 응답은 메모리에서만
쓰며, 가계부 DB와 수집 설정을 수정하지 않습니다. 아이디·암호를 명령 인자로 받지 않습니다.
"""

from datetime import datetime, timedelta
from contextlib import contextmanager
import getpass
import os
import re
from pathlib import Path
import sys
import termios
import warnings
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codef_connection import ConnectionCheckError, configuration, issue_token
from codef_card import (
    CARD_OPTIONS,
    CardCheckError,
    approval_payload,
    query_approvals,
    summarize,
    validate_lotte_name,
)


@contextmanager
def utf8_terminal_input():
    """Linux TTY가 한글 지우기에서 UTF-8의 일부 바이트만 삭제하지 않도록 합니다.

    Docker/모바일 SSH의 IUTF8 설정이 꺼져 있으면 한글을 지울 때 잘린 바이트가 남을 수
    있습니다. getpass의 숨김 입력은 유지하고 입력 종료 시 원래 터미널 설정을 복원합니다.
    """
    descriptor, original = None, None
    try:
        try:
            descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
            original = termios.tcgetattr(descriptor)
            updated = original.copy()
            # Python이 상수를 제공하지 않는 Linux에서도 커널의 IUTF8 플래그를 사용합니다.
            flag = getattr(termios, "IUTF8", 0x4000 if sys.platform.startswith("linux") else 0)
            updated[0] |= flag
            termios.tcsetattr(descriptor, termios.TCSANOW, updated)
        except (OSError, termios.error):
            pass
        yield
    finally:
        if descriptor is not None:
            try:
                if original is not None:
                    termios.tcsetattr(descriptor, termios.TCSANOW, original)
            finally:
                os.close(descriptor)


def read_hidden(prompt):
    """깨진 문자 입력은 원문 없이 다시 받습니다. 임의 인코딩 변환이나 표시 입력은 하지 않습니다."""
    for _ in range(3):
        try:
            with utf8_terminal_input(), warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                return getpass.getpass(prompt)
        except UnicodeDecodeError:
            print(
                "한글 입력을 읽지 못했습니다. 입력 내용을 다시 복사해 한 번에 붙여 넣고 Enter를 누르세요."
            )
        except getpass.GetPassWarning:
            raise CardCheckError(
                "입력을 숨길 수 없는 터미널입니다. 대화형 SSH에서 다시 실행해 주세요."
            ) from None
    raise CardCheckError("문자 인코딩 오류로 중단했습니다. 입력값을 전송하지 않았습니다.")


def selected_card():
    """카드사 이름만 인자로 받습니다. 알 수 없는 인자 원문은 출력하지 않습니다."""
    arguments = sys.argv[1:]
    if not arguments:
        return "samsung"
    if len(arguments) == 2 and arguments[0] == "--card" and arguments[1] in CARD_OPTIONS:
        return arguments[1]
    choices = "|".join(CARD_OPTIONS)
    raise CardCheckError(
        f"사용법: verify-codef-card.py [--card {choices}]. 로그인 정보는 인자로 넣지 마세요."
    )


def read_lotte_name():
    """다른 본인 정보를 받기 전에 이름부터 검증하고, 잘못된 이름만 최대 세 번 다시 받습니다."""
    for _ in range(3):
        raw = read_hidden("롯데카드에 등록된 본인 이름 (입력은 숨김): ")
        try:
            value = validate_lotte_name(raw)
            print("이름 입력 형식: 정상")
            return value
        except CardCheckError as error:
            print(str(error))
    raise CardCheckError("이름 입력을 완료하지 못했습니다. CODEF로 전송하지 않았습니다.")


def answer_challenge(method):
    """공급자 원문 대신 정해진 안내만 표시하고 인증번호도 화면에 노출하지 않습니다."""
    if method == "simpleAuth":
        input("휴대폰으로 받은 카드사 인증을 승인한 뒤 Enter를 누르세요 (취소: Ctrl+C): ")
        return "1"
    label = "문자" if method == "smsAuthNo" else "이메일"
    print(f"CODEF에서 {label} 추가 인증을 요청했습니다. 받은 인증번호로 같은 조회를 이어갑니다.")
    pattern = r"[0-9]{1,32}" if method == "smsAuthNo" else r"[A-Za-z0-9]{1,32}"
    while True:
        value = read_hidden(f"{label} 인증번호 (입력은 숨김, 취소 Ctrl+C): ").strip()
        if re.fullmatch(pattern, value):
            return value
        print(
            "인증번호가 비어 있거나 형식이 다릅니다. 받은 번호를 확인한 뒤 입력하고 Enter를 누르세요. 아직 전송하지 않았습니다."
        )


def read_card_payload(settings, card, start, end):
    """수동 시험·최초 연결에서 같은 숨김 입력과 검증을 사용합니다. 반환값은 메모리 전용입니다."""
    label = CARD_OPTIONS[card][1]
    login_id = read_hidden(f"{label} 홈페이지 아이디 (입력은 숨김): ").strip()
    password = read_hidden(f"{label} 홈페이지 비밀번호 (입력은 숨김): ")
    card_number, card_pin, user_name, identity = "", "", "", ""
    phone_no, telecom = "", ""
    if card == "lotte":
        user_name = read_lotte_name()
        identity = read_hidden("생년월일 6자리 + 주민번호 뒷자리 첫 숫자, 총 7자리 (입력은 숨김): ")
        phone_no = read_hidden("본인 명의 휴대폰번호 (입력은 숨김): ")
        print("통신사: 0 SKT / 1 KT / 2 LG U+ / 3 알뜰폰 SKT망 / 4 알뜰폰 KT망 / 5 알뜰폰 LG U+망")
        telecom = read_hidden("가입한 통신사 숫자 0~5 (입력은 숨김): ").strip()
    if card == "hyundai":
        print(
            "현대는 카드 소지 확인도 필요합니다. 카드번호는 TLS로, PIN은 RSA 암호화해 CODEF로 전송합니다."
        )
        print(
            "카드 비밀번호 3회 오류 시 이용이 제한될 수 있습니다. 확실하지 않으면 Ctrl+C로 중단하세요."
        )
        card_number = read_hidden("현대카드 전체 카드번호 (숫자만, 입력은 숨김): ").strip()
        card_pin = read_hidden("현대카드 비밀번호 4자리 (입력은 숨김): ")
    payload = approval_payload(
        settings,
        card,
        login_id,
        password,
        start,
        end,
        card_number=card_number,
        card_pin=card_pin,
        user_name=user_name,
        identity=identity,
        phone_no=phone_no,
        telecom=telecom,
    )
    return payload


def main():
    """운영자가 직접 실행할 때만 외부 조회합니다. 잘못된 로그인은 반복하지 않습니다."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("대화형 터미널(-it)에서 실행해 주세요. 로그인 정보를 명령에 넣지 마세요.")
        return 1
    try:
        card = selected_card()
        label = CARD_OPTIONS[card][1]
        settings = configuration()
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        start, end = (today - timedelta(days=6)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
        print(f"{label} 데모 실조회 시험 · 한국 날짜 {start}~{end}")
        print("카드사 홈페이지 로그인 정보가 CODEF로 전송됩니다. 명세서 암호와는 다릅니다.")
        print("조회 결과는 건수만 표시하며 지출·연결 정보는 저장하지 않습니다. 취소: Ctrl+C")
        if card == "lotte":
            print(
                "롯데는 이름·본인 확인용 7자리·휴대폰·통신사를 TLS로 CODEF에 전송합니다. 저장·출력하지 않습니다. 전체 주민번호는 입력하지 마세요."
            )
        payload = read_card_payload(settings, card, start, end)
        token = issue_token(settings)
        print("승인·취소 내역 조회 중입니다. 카드사 응답까지 수 분 걸릴 수 있습니다.", flush=True)
        result = summarize(query_approvals(token, payload, answer_challenge))
        print("CODEF 승인내역 API 응답: 성공")
        print(
            f"전체 {result['total']}건 · 승인 {result['approval']}건 · 취소 {result['cancel']}건 · "
            f"부분취소 {result['partial']}건 · 거절 {result['declined']}건 · 확인 필요 {result['invalid']}건"
        )
        print(
            f"같은 기간 {label} 앱의 전체 카드 내역 건수와 비교해 주세요. 0건은 조회 실패를 뜻하지 않습니다."
        )
        print("일치해도 자동 수집·지출 저장은 아직 시작하지 않습니다.")
        return 2 if result["invalid"] else 0
    except (ConnectionCheckError, CardCheckError, ValueError) as error:
        print(str(error))
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n조회 시험을 중단했습니다. 저장하지 않았습니다.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
