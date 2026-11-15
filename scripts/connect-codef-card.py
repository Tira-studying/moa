"""카드사를 개인 모아 계정에 연결하고 연결 ID만 암호화해 저장하는 터미널 도구입니다.

비밀번호·PIN·본인 정보는 최초 연결 요청 동안만 메모리에서 사용합니다. 기존 연결은
아이디·비밀번호 없이 조회를 재검증합니다. 인증 실패 시 사용자가 교체를 선택하거나
--replace를 명시하면 새 정보를 받고, 등록·인증 실패 시 기존 연결을 보존합니다. --init-key는 쓰기용 비밀 볼륨에서 최초 한 번만 실행합니다.
"""

from datetime import datetime, timedelta
import os
import re
from pathlib import Path
import runpy
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from codef_card import CARD_OPTIONS, CardCheckError, query_approvals
from codef_connection import ConnectionCheckError
from codef_direct import collect_direct
from codef_sync import (
    KST,
    NORMALIZATION_VERSION,
    Client,
    SyncError,
    collect,
    latest_slot,
    save_link,
    storage_key,
    touch_revision,
)


def init_key():
    """기존 암호화 키를 교체하지 않고 600 권한의 키를 최초 생성합니다."""
    root = Path(os.environ.get("MOA_CODEF_SETUP_DIR", "/setup-secrets"))
    root.mkdir(parents=True, exist_ok=True)
    path = root / "codef-storage.key"
    if path.exists():
        Fernet(path.read_bytes().strip())
        print("기존 CODEF 암호화 키를 유지합니다.")
        return
    with tempfile.NamedTemporaryFile(dir=root) as temporary:
        temporary.write(Fernet.generate_key())
        temporary.flush()
        os.fsync(temporary.fileno())
        os.chmod(temporary.name, 0o600)
        if os.getuid() == 0:
            os.chown(temporary.name, 1000, 1000)
        os.link(temporary.name, path)
    print("CODEF 저장 암호화 키를 준비했습니다. 이 키는 연결 ID 복구에 필요하므로 보관하세요.")


class RegistrationError(SyncError):
    """기관별 등록 실패입니다. 원문이나 잘못 입력한 로그인 값은 보관하지 않습니다."""

    def __init__(self, message, *, code=None, additional_auth_started=False):
        # 안전하게 구성한 기관 상세 안내는 유지하되 포괄적 등록 실패를 재입력으로 돌리지 않습니다.
        super().__init__(message)
        self.code = code
        self.additional_auth_started = additional_auth_started
        self.allow_login_retry = not additional_auth_started and code not in (
            "CF-04000",
            "CF-12401",
        )


def choose_new_login(operator):
    """무한 로그인 재시도 대신 사용자가 선택한 경우 한 번만 새 정보를 받습니다."""
    print(
        "인증·등록 확인이 필요합니다. 새 로그인 정보로 교체하려면 2, 종료하려면 Enter를 누르세요."
    )
    print(
        "다시 등록하면 CODEF 요청이 발생합니다. 비밀번호는 저장하지 않으며 등록 실패 시 기존 연결을 유지합니다."
    )
    return (
        operator["read_hidden"]("선택 (2: 새 아이디·비밀번호 입력 / Enter: 종료): ").strip() == "2"
    )


def register_account(client, settings, token, card, operator):
    """기관별 등록 성공을 확인한 연결 ID만 반환하고, 실패 원문은 노출하지 않습니다."""
    print("선택한 모아 계정에 카드사를 연결합니다. 최초 로그인 정보는 CODEF로 전송합니다.")
    print("로컬에는 연결 ID만 암호화해 저장하며, 검증된 최근 30일 지출은 원장에 자동 저장합니다.")
    today = datetime.now(KST).date()
    start, end = (today - timedelta(days=6)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
    payload = operator["read_card_payload"](settings, card, start, end)
    fields = (
        "organization",
        "loginType",
        "id",
        "password",
        "userName",
        "identity",
        "phoneNo",
        "telecom",
        "loginCardNo",
        "cardPassword",
    )
    entry = {name: payload[name] for name in fields if name in payload}
    if card == "hyundai":
        # 승인 상품은 loginCardNo, 계정 등록은 cardNo를 요구합니다. 같은 번호를
        # 등록 요청에서만 이름을 바꿔 보내며, 원본 상품 요청과 암호화 PIN은 유지합니다.
        entry["cardNo"] = entry.pop("loginCardNo")
    entry.update(countryCode="KR", businessType="CD", clientType="P")
    print(
        "CODEF 카드사 계정 등록 요청 중입니다. 추가 인증을 이어갈 정보가 오면 번호 입력을 기다립니다."
    )
    additional_auth_started = False

    def answer(method):
        nonlocal additional_auth_started
        additional_auth_started = True
        return operator["answer_challenge"](method)

    try:
        data = query_approvals(
            token,
            {"accountList": [entry]},
            answer,
            organization=CARD_OPTIONS[card][0],
            sender=lambda token, body: client.send(token, body, "register"),
        )
    except CardCheckError as error:
        if error.code:
            raise RegistrationError(
                str(error), code=error.code, additional_auth_started=additional_auth_started
            ) from None
        raise
    success = data.get("successList") if isinstance(data, dict) else None
    if (
        not isinstance(success, list)
        or data.get("errorList")
        or not any(
            isinstance(row, dict)
            and row.get("organization") == CARD_OPTIONS[card][0]
            and row.get("businessType") == "CD"
            and row.get("clientType") == "P"
            and row.get("code") == "CF-00000"
            for row in success
        )
    ):
        errors = data.get("errorList", []) if isinstance(data, dict) else []
        codes = (
            [row.get("code") for row in errors if isinstance(row, dict)]
            if isinstance(errors, list)
            else []
        )
        code = next(
            (
                code
                for code in codes
                if isinstance(code, str) and re.fullmatch(r"CF-[0-9]{5}", code)
            ),
            None,
        )
        raise RegistrationError(
            "카드사 등록 성공을 확인하지 못했습니다. 기존 로컬 연결을 유지합니다."
            + (f" [코드: {code}]" if code else ""),
            code=code,
            additional_auth_started=additional_auth_started,
        )
    return data.get("connectedId")


def restore_failed_replacement(connect, previous, candidate):
    """새 연결의 인증 검증이 실패하면 기존 연결을 복원합니다. 동시 교체·중지는 덮어쓰지 않습니다."""
    if not previous:
        return False
    fields = (
        "cipher",
        "app_digest",
        "enabled",
        "state",
        "message",
        "error_code",
        "last_success",
        "last_attempt",
        "requested",
        "requested_done",
        "card_auth_cipher",
    )
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE codef_links SET "
                + ",".join(name + "=?" for name in fields)
                + ",version=version+1 WHERE owner_id=? AND card=? AND version=?",
                tuple(previous[name] for name in fields)
                + (candidate["owner_id"], candidate["card"], candidate["version"]),
            ).rowcount
            if changed:
                touch_revision(db, candidate["owner_id"], "실패한 CODEF 새 연결 취소")
            return bool(changed)
    finally:
        db.close()


def main():
    """활성 모아 계정을 명시적으로 선택하고 성공한 등록만 로컬 연결로 보관합니다."""
    try:
        args = sys.argv[1:]
        if args == ["--init-key"]:
            init_key()
            return 0
        replace = args[-1:] == ["--replace"]
        direct = args[-1:] == ["--direct"]
        if replace or direct:
            args = args[:-1]
        if (
            len(args) != 4
            or args[0] != "--card"
            or args[1] not in CARD_OPTIONS
            or args[2] != "--user"
            or not sys.stdin.isatty()
        ):
            print(
                "사용법: connect-codef-card.py --card 카드사 --user 모아아이디 [--replace 또는 --direct]"
            )
            return 1
        card, username = args[1], args[3]
        print("승인 내역 처리 버전:", NORMALIZATION_VERSION)
        storage_key()
        from app import connect

        db = connect()
        try:
            user = db.execute(
                "SELECT id FROM users WHERE username=? AND active=1", (username,)
            ).fetchone()
            if not user:
                raise SyncError("활성 모아 아이디를 확인해 주세요.")
            owner = user[0]
            existing = db.execute(
                "SELECT * FROM codef_links WHERE owner_id=? AND card=?", (owner, card)
            ).fetchone()
        finally:
            db.close()
        operator = runpy.run_path(str(Path(__file__).with_name("verify-codef-card.py")))
        client = Client(connect)
        if direct:
            count = collect_direct(connect, client, owner, card, operator)
            print(
                f"수동 조회·저장 확인: 승인 묶음 {count}건. 불확실한 내역은 확인 필요로 남깁니다."
            )
            print(
                "모아의 사용 내역·계정의 CODEF 검토 목록을 새로고침하세요. 예약 수집 연결은 변경하지 않았습니다."
            )
            return 0
        settings, token = client.credentials()
        use_existing = bool(existing and not replace)
        for attempt in range(2):
            try:
                if use_existing:
                    from cryptography.fernet import InvalidToken

                    try:
                        cid = Fernet(storage_key()).decrypt(existing["cipher"].encode()).decode()
                    except (InvalidToken, UnicodeError):
                        raise SyncError(
                            "기존 연결을 복호화하지 못했습니다. 원래 암호화 키를 확인해 주세요."
                        ) from None
                    print("기존 암호화 연결 ID로 조회를 다시 확인합니다.")
                else:
                    cid = register_account(client, settings, token, card, operator)
            except RegistrationError as error:
                print(str(error))
                if (
                    attempt == 0
                    and error.allow_login_retry
                    and error.code not in ("CF-12834", "CF-03002", "CF-04034")
                    and choose_new_login(operator)
                ):
                    use_existing = False
                    continue
                if error.additional_auth_started or error.code in ("CF-03002", "CF-04034"):
                    print("추가 인증 확인이 필요해 이 카드의 처리를 중단했습니다.")
                    return 3
                if error.code == "CF-04000":
                    print(
                        "계정 등록을 반복하지 않았습니다. 수동 조회·저장은 --direct로 별도 실행할 수 있습니다. 예약 수집을 활성화하지는 않습니다."
                    )
                return 1
            db = connect()
            try:
                previous = db.execute(
                    "SELECT * FROM codef_links WHERE owner_id=? AND card=?", (owner, card)
                ).fetchone()
                previous = dict(previous) if previous else None
            finally:
                db.close()
            save_link(connect, owner, card, cid, settings, verifying=True)
            del cid
            db = connect()
            try:
                link = dict(
                    db.execute(
                        "SELECT * FROM codef_links WHERE owner_id=? AND card=?", (owner, card)
                    ).fetchone()
                )
            finally:
                db.close()
            print("연결 ID로 승인 내역을 확인합니다.")
            additional_auth_started = False

            def answer(method):
                nonlocal additional_auth_started
                additional_auth_started = True
                return operator["answer_challenge"](method)

            try:
                collect(connect, client, link, "enroll:" + str(time.time_ns()), answer)
            except (KeyboardInterrupt, EOFError):
                if not use_existing:
                    restore_failed_replacement(connect, previous, link)
                raise
            db = connect()
            try:
                row = dict(
                    db.execute(
                        "SELECT state,message,error_code FROM codef_links WHERE owner_id=? AND card=?",
                        (owner, card),
                    ).fetchone()
                )
                with db:
                    db.execute(
                        "INSERT OR IGNORE INTO codef_jobs(owner_id,card,slot,state,created) VALUES(?,?,?,'covered',?)",
                        (owner, card, latest_slot(), int(time.time())),
                    )
            finally:
                db.close()
            print("연결 상태:", row["state"], "/", row["message"])
            if (
                not use_existing
                and row["state"] not in ("ready", "needs_review")
                and restore_failed_replacement(connect, previous, link)
            ):
                print(
                    "새 연결 검증을 완료하지 못해 기존 연결을 복원했습니다. 기존 지출은 변경하지 않았습니다."
                )
            if row["state"] == "ready":
                return 0
            if row["state"] == "needs_review":
                print(
                    "로그인 정보 오류가 아니라 조회 내역 검증 보류입니다. 연결 ID를 유지하며 새 비밀번호를 요구하지 않습니다."
                )
                return 2
            if (
                attempt == 0
                and not additional_auth_started
                and row["state"] == "needs_auth"
                and row["error_code"]
                not in ("CF-12834", "CF-03002", "CF-04034", "CF-04000", "CF-12401")
                and choose_new_login(operator)
            ):
                use_existing = False
                continue
            if additional_auth_started or row["error_code"] in ("CF-03002", "CF-04034"):
                return 3
            return 2
    except (SyncError, CardCheckError, ConnectionCheckError) as error:
        print(str(error))
        return 1
    except (OSError, ValueError):
        print("연결 준비를 완료하지 못했습니다. 비밀 볼륨 권한·키 형식·저장 공간을 확인해 주세요.")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n연결을 중단했습니다. 로그인 정보는 저장하지 않았습니다.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
