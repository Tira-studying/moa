"""현대 자동 조회용 암호문 재사용을 검증하고 명시적으로 비밀 볼륨에 보관합니다.

카드번호·PIN은 TTY 숨김 입력으로 한 번만 받습니다. 같은 RSA 암호문으로 두 요청이
성공한 뒤에만 카드번호와 암호문을 Fernet으로 감싸 저장합니다. DB에는 파일 참조만
두며 저장 후 작업자와 같은 경로의 30일 조회가 성공해야 ready로 바뀝니다.
"""

from datetime import datetime
import sqlite3
import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet, InvalidToken

import codef_card_auth as card_auth
from codef_card import CardCheckError, encrypt_password
from codef_connection import ConnectionCheckError, config_digest, configuration
from codef_sync import (
    Client,
    KST,
    SyncError,
    check_collection_configuration,
    collect,
    latest_slot,
    storage_key,
    touch_revision,
)


def current_link(connect, username):
    """운영자가 명시한 활성 모아 계정의 현대 연결만 선택합니다."""
    db = connect()
    try:
        row = db.execute(
            "SELECT l.* FROM codef_links l JOIN users u ON u.id=l.owner_id "
            "WHERE u.username=? AND u.active=1 AND l.card='hyundai'",
            (username,),
        ).fetchone()
        if not row:
            raise SyncError("활성 모아 계정의 기존 현대 연결을 먼저 확인해 주세요.")
        return dict(row)
    finally:
        db.close()


def require_current(db, link, *, enabled=True):
    """계정 해제·연결 교체·수집 중지는 인증 설정으로 되돌리지 않습니다."""
    row = db.execute(
        "SELECT l.* FROM codef_links l JOIN users u ON u.id=l.owner_id "
        "WHERE l.owner_id=? AND l.card='hyundai' AND l.version=? AND u.active=1",
        (link["owner_id"], link["version"]),
    ).fetchone()
    if not row or (enabled and not row["enabled"]):
        raise SyncError("계정·연결·수집 설정이 바뀌어 현대 인증 설정을 반영하지 않았습니다.")
    return dict(row)


def configure(connect, client, link, settings, number, pin_cipher):
    """메모리 재사용 검증 → 새 암호화 파일 → DB 참조 확정 → 실제 수집 순서입니다."""
    card_auth.validate_values(number, pin_cipher)
    initial_key = storage_key()
    expected_digest = config_digest(settings)
    if link["app_digest"] != expected_digest:
        raise SyncError("CODEF 앱 설정과 기존 현대 연결이 다릅니다. 연결을 먼저 확인해 주세요.")
    try:
        connected = Fernet(initial_key).decrypt(link["cipher"].encode()).decode()
    except (InvalidToken, UnicodeError):
        raise SyncError("기존 현대 연결을 읽지 못했습니다. 원래 저장 키를 확인해 주세요.") from None

    def guard():
        check_collection_configuration(initial_key, expected_digest)
        db = connect()
        try:
            require_current(db, link)
        finally:
            db.close()

    guard()
    current_settings, token = client.credentials()
    if config_digest(current_settings) != expected_digest:
        raise SyncError("처리 중 CODEF 앱 설정이 바뀌어 검증을 중단했습니다.")
    day = datetime.now(KST).strftime("%Y%m%d")
    print(
        "같은 PIN 암호문으로 두 번의 승인 조회를 검증합니다. 첫 실패에서 중단하며 아직 저장하지 않습니다.",
        flush=True,
    )
    card_auth.verify_reuse(client, token, number, pin_cipher, connected, day, guard)
    print("두 번의 암호문 재사용 검증 성공. 승인하신 암호화 보관 설정을 반영합니다.", flush=True)
    guard()
    ref = card_auth.save(link, connected, settings, initial_key, number, pin_cipher)
    candidate = {**link, "card_auth_ref": ref, "card_auth_cipher": ""}
    try:
        # 쓰기 마운트와 읽기 마운트가 같은 볼륨인지 DB 변경 전에 확인합니다.
        card_auth.load(candidate, connected, settings, initial_key)
        db = connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                require_current(db, link)
                check_collection_configuration(initial_key, expected_digest)
                db.execute(
                    "UPDATE codef_links SET card_auth_ref=?,card_auth_cipher='',version=version+1,state='verifying',"
                    "message='현대 자동 조회 인증 설정 검증 중',error_code='' WHERE owner_id=? AND card='hyundai'",
                    (ref, link["owner_id"]),
                )
                touch_revision(db, link["owner_id"], "현대 자동 조회 암호화 인증 설정")
        finally:
            db.close()
    except BaseException:
        card_auth.remove(link, ref)
        raise
    old_ref = link.get("card_auth_ref", "")
    if old_ref:
        card_auth.remove(link, old_ref)
    db = connect()
    try:
        candidate = dict(
            db.execute(
                "SELECT * FROM codef_links WHERE owner_id=? AND card='hyundai'", (link["owner_id"],)
            ).fetchone()
        )
    finally:
        db.close()
    if candidate["version"] != link["version"] + 1 or candidate["card_auth_ref"] != ref:
        raise SyncError("확인 중 다른 작업에서 현대 연결이 바뀌어 추가 조회를 진행하지 않았습니다.")
    print("비밀 파일을 다시 읽어 작업자와 같은 최근 30일 조회·저장 경로를 확인합니다.", flush=True)
    collect(connect, client, candidate, "card-auth:" + str(time.time_ns()))
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            require_current(db, candidate)
            row = dict(
                db.execute(
                    "SELECT state,message,version FROM codef_links WHERE owner_id=? AND card='hyundai'",
                    (link["owner_id"],),
                ).fetchone()
            )
            if row["state"] == "ready":
                db.execute(
                    "INSERT OR IGNORE INTO codef_jobs(owner_id,card,slot,state,created) VALUES(?,?,?,'covered',?)",
                    (link["owner_id"], "hyundai", latest_slot(), int(time.time())),
                )
        return row
    finally:
        db.close()


def disable(connect, link):
    """암호화 추가 인증 설정만 해제하고 연결 ID·원장·다른 카드 설정은 보존합니다."""
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            current = require_current(db, link, enabled=False)
            db.execute(
                "UPDATE codef_links SET card_auth_ref='',card_auth_cipher='',version=version+1,state='needs_auth',"
                "message='현대 자동 조회 추가 인증 설정이 해제됐습니다.',error_code='' "
                "WHERE owner_id=? AND card='hyundai'",
                (link["owner_id"],),
            )
            touch_revision(db, link["owner_id"], "현대 자동 조회 추가 인증 해제")
    finally:
        db.close()
    card_auth.remove(link, current["card_auth_ref"])


def main():
    """비밀값을 인자로 받지 않고, 활성 개인 계정과 TTY를 검사한 뒤 숨김 입력합니다."""
    args = sys.argv[1:]
    if (
        len(args) not in (2, 3)
        or args[0] != "--user"
        or (len(args) == 3 and args[2] != "--disable")
    ):
        print(
            "사용법: configure-codef-card-auth.py --user 모아아이디 [--disable]. 비밀값은 인자로 넣지 마세요."
        )
        return 1
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("대화형 터미널에서 실행해 주세요. PIN은 채팅·명령 인자로 받지 않습니다.")
        return 1
    try:
        from app import connect
        import runpy

        link = current_link(connect, args[1])
        if len(args) == 3:
            disable(connect, link)
            print("현대 추가 인증 암호문을 해제했습니다. 기존 연결 ID·지출은 유지합니다.")
            return 0
        settings = configuration()
        operator = runpy.run_path(str(Path(__file__).with_name("verify-codef-card.py")))
        print("현대 자동 수집용 인증 설정입니다. 홈페이지 아이디·비밀번호는 다시 받지 않습니다.")
        print(
            "같은 PIN 암호문의 재사용이 두 번 성공할 때만 카드번호와 PIN 암호문을 암호화 보관합니다."
        )
        print("PIN 평문은 저장하지 않지만 암호문도 인증에 재사용되는 비밀정보입니다. 취소: Ctrl+C")
        number = operator["read_hidden"]("현대 전체 카드번호 (숫자만, 입력 숨김): ").strip()
        pin = operator["read_hidden"]("현대 카드 비밀번호 4자리 (입력 숨김): ")
        if (
            not card_auth.valid_number(number)
            or not isinstance(pin, str)
            or len(pin) != 4
            or not pin.isascii()
            or not pin.isdigit()
        ):
            raise card_auth.CardAuthError(
                "카드번호·PIN 형식을 확인해 주세요. 금융 요청은 보내지 않았습니다."
            )
        encrypted = encrypt_password(settings["public_key"], pin)
        pin = None
        result = configure(connect, Client(connect), link, settings, number, encrypted)
        print("현대 연결 상태:", result["state"], "/", result["message"])
        if result["state"] == "ready":
            print("현대 자동 수집 준비 완료. 실행 중인 작업자가 한국시간 하루 4회 조회합니다.")
            return 0
        print("자동 수집 성공으로 표시하지 않았습니다. 상태 안내를 확인해 주세요.")
        return 2
    except (SyncError, CardCheckError, ConnectionCheckError, card_auth.CardAuthError) as error:
        print(str(error))
        return 1
    except (OSError, ValueError, sqlite3.Error):
        print("현대 인증 설정을 완료하지 못했습니다. 비밀 볼륨 권한·키 형식을 확인해 주세요.")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("현대 인증 설정을 중단했습니다. 같은 카드를 자동 재시도하지 않습니다.")
        return 130
    except Exception:
        print(
            "현대 인증 설정 중 오류가 발생했습니다. 연결 상태를 확인해 주세요. 비밀값은 출력하지 않습니다."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
