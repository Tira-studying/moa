"""TTY 숨김 입력으로 우리은행 한 계좌만 등록·검증합니다.

사용자가 암호화 저장에 동의한 뒤 CODEF로 로그인·계좌 확인 값을 전송합니다.
등록/조회 실패 시 기존 연결과 입출금을 보존하며 로그인 오류를 재시도하지 않습니다.
"""

from datetime import datetime, timedelta
from pathlib import Path
import re
import runpy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codef_bank import (
    account_tag,
    normalize,
    query,
    registration_id,
    save_verified,
    transaction_payload,
    woori_auth_fields,
)
from codef_card import CardCheckError, encrypt_password
from codef_sync import (
    AuthenticationRequired,
    Client,
    KST,
    SyncError,
    storage_key,
    require_request_budget,
)


def query_transactions(client, token, payload, operator):
    """기관 통신 오류 때만 사용자가 고른 경우 같은 연결로 딱 한 번 더 조회합니다.

    첫 등록이 성공한 뒤의 읽기 요청만 다룹니다. 로그인/계좌 인증 오류는 선택지를
    표시하지 않고 즉시 중단하며 작업자의 자동 재시도 정책도 바꾸지 않습니다.
    """
    try:
        return query(client, token, payload, answer=operator["answer_challenge"])
    except CardCheckError as error:
        if error.code != "CF-12200":
            raise
        print(str(error))
        require_request_budget(client.connect)
        print("로그인 등록은 확인됐습니다. 우리은행 홈페이지 접속 상태를 확인한 뒤 선택하세요.")
        print("같은 연결·계좌 확인값을 메모리에서 사용해 CODEF에 입출금 조회를 한 번 더 보냅니다.")
        choice = operator["read_hidden"](
            "1: 같은 입력으로 조회 1회 재요청 / Enter: 종료 (숨김): "
        ).strip()
        if choice != "1":
            raise
    print("선택한 입출금 조회를 한 번 더 요청합니다. 추가 오류에서는 중단합니다.")
    return query(client, token, payload, answer=operator["answer_challenge"])


def main():
    """비밀번호 평문을 저장하지 않고 최초 조회 성공까지 확인한 연결만 활성화합니다."""
    if len(sys.argv) != 3 or sys.argv[1] != "--user" or not sys.stdin.isatty():
        print("사용법: connect-codef-bank.py --user 모아아이디 (대화형 터미널 전용)")
        return 1
    try:
        from app import connect

        db = connect()
        try:
            user = db.execute(
                "SELECT id FROM users WHERE username=? AND active=1", (sys.argv[2],)
            ).fetchone()
            if not user:
                raise SyncError("활성 모아 아이디를 확인해 주세요.")
            owner = user[0]
        finally:
            db.close()
        # 새 CLI는 토큰 발급·단일 등록·최초 조회에 기본 3회가 필요합니다.
        # 예산이 없으면 개인정보부터 입력받지 않습니다. 각 실제 호출도 원자적으로 다시 검사합니다.
        require_request_budget(connect, minimum=3)
        key = storage_key()
        operator = runpy.run_path(str(Path(__file__).with_name("verify-codef-card.py")))
        hidden = operator["read_hidden"]
        print("모아 우리은행 연결 · 본인 원화 입출금계좌 한 곳 · 최근 30일")
        print(
            "우리은행 인터넷뱅킹 아이디·비밀번호, 계좌번호·계좌비밀번호·생년월일을 CODEF로 전송합니다."
        )
        print(
            "CODEF가 로그인 정보를 보관하는 연결 ID를 발급합니다. 이체·결제 요청은 하지 않습니다."
        )
        print(
            "자동 조회를 위해 연결 ID·계좌번호·생년월일·RSA 암호화 계좌비밀번호를 모아 DB에 다시 암호화해 저장합니다."
        )
        print(
            "복호화 키는 기존 별도 비밀 볼륨에 있습니다. 비밀번호 평문과 인증번호는 저장하지 않습니다."
        )
        print(
            "입출금은 별도로 보관하며, 화면에서 실제 지출로 확인한 출금만 소비 합계에 반영합니다."
        )
        print(
            "우리은행은 로그인 등록에도 계좌번호·계좌비밀번호 확인이 필요합니다. 조회할 본인 계좌를 함께 사용합니다."
        )
        print(
            "우리은행 비밀번호 오류 3회 시 제한될 수 있습니다. 확실하지 않으면 Ctrl+C로 종료하세요."
        )
        if (
            hidden("위 전송·암호화 저장에 동의하면 '연결' 입력 (숨김, Enter: 취소): ").strip()
            != "연결"
        ):
            print("취소했습니다. 우리은행 금융 요청을 보내지 않았습니다.")
            return 0
        login = hidden("우리은행 인터넷뱅킹 아이디 (숨김): ").strip()
        password = hidden("우리은행 인터넷뱅킹 비밀번호 (숨김): ")
        birth = hidden("생년월일 8자리 YYYYMMDD (숨김): ")
        if not login or len(login) > 200 or not password or len(password) > 200:
            raise SyncError("인터넷뱅킹 로그인 입력 형식을 확인해 주세요. 전송하지 않았습니다.")
        if not re.fullmatch(r"[0-9]{8}", birth):
            raise SyncError("생년월일 8자리 형식을 확인해 주세요. 전송하지 않았습니다.")
        try:
            datetime.strptime(birth, "%Y%m%d")
        except ValueError:
            raise SyncError("생년월일 날짜를 확인해 주세요. 전송하지 않았습니다.") from None
        account = re.sub(
            r"[-\s]", "", hidden("조회·로그인 확인에 사용할 본인 원화 계좌번호 (숨김): ")
        )
        pin = hidden("해당 계좌 비밀번호 4자리 (숨김): ")
        if not re.fullmatch(r"[0-9]{10,16}", account) or not re.fullmatch(r"[0-9]{4}", pin):
            raise SyncError(
                "계좌번호·계좌비밀번호 형식을 확인해 주세요. 금융 요청을 전송하지 않았습니다."
            )
        db = connect()
        try:
            if not db.execute("SELECT 1 FROM users WHERE id=? AND active=1", (owner,)).fetchone():
                raise SyncError("모아 계정 접근이 해제되어 금융 요청을 보내지 않았습니다.")
        finally:
            db.close()
        client = Client(connect)
        settings, token = client.credentials()
        encrypted_password = encrypt_password(settings["public_key"], password)
        encrypted_pin = encrypt_password(settings["public_key"], pin)
        del password, pin
        print("CODEF 우리은행 단일 계정 등록 중입니다. 로그인 오류는 자동 재시도하지 않습니다.")
        entry = dict(
            countryCode="KR",
            businessType="BK",
            clientType="P",
            organization="0020",
            loginType="1",
            id=login,
            password=encrypted_password,
            **woori_auth_fields(account, encrypted_pin, birth),
        )
        cid = registration_id(
            query(client, token, {"accountList": [entry]}, "register", operator["answer_challenge"])
        )
        entry.clear()
        del login, encrypted_password
        print("우리은행 로그인 등록을 확인했습니다. 같은 계좌로 입출금을 검증합니다.")
        db = connect()
        try:
            row = db.execute(
                "SELECT * FROM bank_links WHERE owner_id=? AND account_tag=?",
                (owner, account_tag(key, account)),
            ).fetchone()
            previous = dict(row) if row else None
        finally:
            db.close()
        fields = dict(
            connectedId=cid, account=account, accountPassword=encrypted_pin, birthDate=birth
        )
        today = datetime.now(KST).date()
        start, end = (today - timedelta(days=29)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
        print("연결 ID로 해당 계좌 최근 30일 입출금을 검증합니다.")
        data = query_transactions(
            client,
            token,
            transaction_payload(fields, start, end),
            operator,
        )
        items = normalize(data, account, start, end, key)
        save_verified(connect, owner, fields, settings, key, items, previous)
        print(
            f"우리은행 연결·저장 완료: 최근 30일 {len(items)}건. 모아 더보기 → 계좌 입출금에서 확인하세요."
        )
        print("이후 기존 CODEF 작업자가 하루 4회 조회하며, 추가 인증·오류 시 멈춥니다.")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\n중단했습니다. 확인되지 않은 새 연결은 저장하지 않습니다.")
        return 130
    except (SyncError, AuthenticationRequired, CardCheckError) as error:
        print(str(error))
        print("새 연결은 저장하지 않았으며 기존 카드·계좌 자료는 유지합니다.")
        return 2
    except Exception:
        print("우리은행 연결을 완료하지 못했습니다. 개인정보를 출력하지 않고 중단합니다.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
