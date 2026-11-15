"""현대·KB 자동 조회에 필요한 카드번호와 CODEF용 PIN 암호문을 보관합니다.

현대는 승인 조회마다 카드번호·PIN을 요구할 수 있고, KB는 하루 첫 조회에서 카드 소지 확인
(CF-12108: 카드번호 + 비밀번호 앞 2자리)을 요구합니다. KB는 사용자가 휴대폰 연결에서 저장을
선택한 경우에만 같은 방식으로 이중 암호화해 DB에 두고, 작업자는 CF-12108을 받았을 때만 한 번 씁니다.

PIN 평문은 받지 않습니다. 공급자 공개키로 이미 암호화한 PIN과 카드번호를 다시
Fernet으로 감싸며, DB에는 무작위 파일 참조만 둡니다. 사용자·연결 ID·앱 키에 묶어
다른 계정이나 교체된 연결에 재사용하지 않습니다. 기존 파일은 읽기 전용으로 유지하며,
웹 연결은 동일한 별도 키로 암호화한 card_auth_cipher를 DB에 보관합니다.
"""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import uuid

from cryptography.fernet import Fernet, InvalidToken

from codef_card import CardCheckError, query_approvals, summarize
from codef_connection import config_digest

LIMIT = 16384
# DB 암호문(card_auth_cipher)으로 자동 조회 인증정보를 둘 수 있는 카드사입니다. 파일 참조는 현대만 씁니다.
SEALED_CARDS = ("hyundai", "kb")


class CardAuthError(Exception):
    """인증정보·경로·공급자 원문이 없는 고정 안내만 전달합니다."""


def directory(*, writable=False):
    """운영 읽기 경로와 운영자가 명시한 쓰기 마운트를 구분합니다."""
    name = "MOA_CODEF_CARD_AUTH_SETUP_DIR" if writable else "MOA_CODEF_CARD_AUTH_DIR"
    default = "/setup-secrets/codef-card-auth" if writable else "/run/moa-secrets/codef-card-auth"
    return Path(os.environ.get(name, default))


def valid_number(number):
    """유효한 형태만 확인하며 실제 번호는 오류 안내에 넣지 않습니다."""
    return isinstance(number, str) and re.fullmatch(r"[0-9]{14,19}", number) is not None


def validate_values(number, pin_cipher):
    """평문 PIN이 저장 API로 들어오지 않도록 RSA 암호문의 형식도 검사합니다."""
    try:
        if not valid_number(number) or not isinstance(pin_cipher, str) or len(pin_cipher) > 4096:
            raise ValueError()
        encoded = base64.b64decode(pin_cipher, validate=True)
        if not 64 <= len(encoded) <= 1024:
            raise ValueError()
    except (ValueError, TypeError):
        raise CardAuthError("현대 자동 조회 인증정보 형식을 확인해 주세요.") from None


def filename(link, ref):
    """DB 참조를 그대로 경로로 쓰지 않고 해당 사용자의 무작위 파일명만 허용합니다."""
    owner = link["owner_id"]
    if (
        type(owner) is not int
        or owner <= 0
        or link["card"] != "hyundai"
        or not isinstance(ref, str)
        or not re.fullmatch(r"hyundai-" + str(owner) + r"-[0-9a-f]{32}\.enc", ref)
    ):
        raise CardAuthError("현대 자동 조회 인증 설정을 다시 확인해 주세요.")
    return ref


def connection_tag(key, connected, card="hyundai"):
    """암호문 안에도 원래 연결 ID 대신 카드사별로 목적을 분리한 HMAC만 넣습니다."""
    return hmac.new(
        key, f"moa-{card}-card-auth\0".encode() + connected.encode(), hashlib.sha256
    ).hexdigest()


def read_blob(link, ref):
    """크기·일반 파일·600 권한을 검사하고 심볼릭 링크는 따라가지 않습니다."""
    try:
        path = directory() / filename(link, ref)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError()
            blob = stream.read(LIMIT + 1)
        if len(blob) > LIMIT:
            raise ValueError()
        return blob
    except (OSError, ValueError):
        raise CardAuthError("현대 자동 조회 비밀 파일의 존재·권한·크기를 확인해 주세요.") from None


def load(link, connected, settings, key):
    """현대의 현재 DB 참조만 복호화하고 조회에 필요한 세 필드만 돌려줍니다."""
    sealed = dict(link).get("card_auth_cipher", "")
    if link["card"] in SEALED_CARDS and sealed:
        return load_sealed(link, connected, settings, key, sealed)
    ref = dict(link).get("card_auth_ref", "")
    if link["card"] != "hyundai" or not ref:
        return {}, None
    blob = read_blob(link, ref)
    try:
        record = json.loads(Fernet(key).decrypt(blob))
        expected = {
            "v": 1,
            "owner": link["owner_id"],
            "card": "hyundai",
            "ref": ref,
            "cid": connection_tag(key, connected),
            "app": config_digest(settings),
        }
        if not isinstance(record, dict) or any(record.get(k) != v for k, v in expected.items()):
            raise ValueError()
        number, encrypted_pin = record["number"], record["pin"]
        validate_values(number, encrypted_pin)
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError):
        raise CardAuthError(
            "현대 자동 조회 인증 설정이 현재 계정·연결·키와 맞지 않습니다."
        ) from None
    return {
        "cardNo": number,
        "loginCardNo": number,
        "cardPassword": encrypted_pin,
    }, hashlib.sha256(blob).hexdigest()


def check_unchanged(link, snapshot):
    """외부 조회 도중 파일이 바뀌거나 사라지면 늦은 결과를 원장에 쓰지 않습니다."""
    if snapshot and snapshot.startswith("db:"):
        current = (
            "db:" + hashlib.sha256(dict(link).get("card_auth_cipher", "").encode()).hexdigest()
        )
        if not hmac.compare_digest(current, snapshot):
            raise CardAuthError("처리 중 현대 자동 조회 인증 설정이 바뀌어 저장하지 않았습니다.")
        return
    if snapshot is not None:
        blob = read_blob(link, link["card_auth_ref"])
        if not hmac.compare_digest(hashlib.sha256(blob).hexdigest(), snapshot):
            raise CardAuthError("처리 중 현대 자동 조회 인증 설정이 바뀌어 저장하지 않았습니다.")


def seal(link, connected, settings, key, number, pin_cipher):
    """웹 연결은 읽기 전용 비밀 볼륨을 유지하고 이중 암호문만 DB에 저장합니다."""
    validate_values(number, pin_cipher)
    if link["card"] not in SEALED_CARDS or type(link["owner_id"]) is not int:
        raise CardAuthError("현대 자동 조회 연결 대상을 확인해 주세요.")
    record = dict(
        v=2,
        owner=link["owner_id"],
        card=link["card"],
        cid=connection_tag(key, connected, link["card"]),
        app=config_digest(settings),
        number=number,
        pin=pin_cipher,
    )
    return Fernet(key).encrypt(json.dumps(record, separators=(",", ":")).encode()).decode()


def load_sealed(link, connected, settings, key, sealed):
    """별도 비밀 키로 복호화한 뒤 사용자·연결·앱 결합을 확인합니다."""
    try:
        if not isinstance(sealed, str) or len(sealed) > LIMIT:
            raise ValueError()
        record = json.loads(Fernet(key).decrypt(sealed.encode()))
        expected = dict(
            v=2,
            owner=link["owner_id"],
            card=link["card"],
            cid=connection_tag(key, connected, link["card"]),
            app=config_digest(settings),
        )
        if not isinstance(record, dict) or any(record.get(k) != v for k, v in expected.items()):
            raise ValueError()
        validate_values(record["number"], record["pin"])
        return (
            dict(cardNo=record["number"], loginCardNo=record["number"], cardPassword=record["pin"]),
            "db:" + hashlib.sha256(sealed.encode()).hexdigest(),
        )
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError):
        raise CardAuthError("현대 자동 조회 암호문이 현재 계정·연결·키와 맞지 않습니다.") from None


def save(link, connected, settings, key, number, pin_cipher):
    """새 비밀 파일을 먼저 만듭니다. DB가 이 참조를 확정하기 전에는 작업자가 사용하지 않습니다."""
    validate_values(number, pin_cipher)
    ref = filename(link, f"hyundai-{link['owner_id']}-{uuid.uuid4().hex}.enc")
    root = directory(writable=True)
    created = False
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError()
        record = dict(
            v=1,
            owner=link["owner_id"],
            card="hyundai",
            ref=ref,
            cid=connection_tag(key, connected),
            app=config_digest(settings),
            number=number,
            pin=pin_cipher,
        )
        blob = Fernet(key).encrypt(json.dumps(record, separators=(",", ":")).encode())
        path = root / ref
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return ref
    except (OSError, ValueError):
        if created:
            try:
                (root / ref).unlink()
            except OSError:
                pass
        raise CardAuthError(
            "현대 자동 조회 비밀 파일을 저장하지 못했습니다. 쓰기 마운트·권한을 확인해 주세요."
        ) from None


def remove(link, ref):
    """DB에서 더 이상 참조하지 않는 해당 사용자의 비밀 파일만 정리합니다."""
    if ref:
        try:
            (directory(writable=True) / filename(link, ref)).unlink(missing_ok=True)
        except OSError:
            raise CardAuthError("이전 현대 자동 조회 비밀 파일을 정리하지 못했습니다.") from None


def verify_reuse(client, token, number, pin_cipher, connected, day, before_request):
    """같은 RSA 암호문으로 두 요청을 확인하고 첫 실패에서는 즉시 멈춥니다. 저장하지 않습니다."""
    validate_values(number, pin_cipher)
    payload = dict(
        organization="0302",
        connectedId=connected,
        cardNo=number,
        loginCardNo=number,
        cardPassword=pin_cipher,
        startDate=day,
        endDate=day,
        orderBy="0",
        inquiryType="1",
        memberStoreInfoType="0",
    )

    def stop_auth(method):
        raise CardCheckError("추가 인증이 필요해 현대 암호문 재사용 검증을 중단했습니다.")

    try:
        for _ in range(2):
            before_request()
            data = query_approvals(token, payload, stop_auth, sender=client.send)
            if summarize(data)["invalid"]:
                raise CardAuthError(
                    "응답 형식 확인이 필요해 현대 자동 인증정보를 저장하지 않았습니다."
                )
    finally:
        payload.clear()
