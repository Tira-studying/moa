"""6개 카드사의 공통 입력·RSA 암호화·승인 조회·추가 인증 검증 도구입니다.

verify-codef-card.py가 입력받은 비밀번호는 RSA로 암호화하고, 필수 본인 정보와 함께
고정된 데모 HTTPS 주소로 조회합니다. 추가 인증만 제한적으로 이어갑니다. 토큰·응답·개인 연결을
디스크나 DB에 저장하지 않으며, 결과는 원문 대신 건수로만 요약합니다.
"""

import base64
from collections import Counter
from datetime import datetime
import http.client
import json
import re
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from codef_connection import DEMO_HOST, NoRedirect, validate_public_key

APPROVAL_URL = DEMO_HOST + "/v1/kr/card/p/account/approval-list"
BODY_LIMIT = 4 * 1024 * 1024
MAX_CHALLENGES = 3
CARD_OPTIONS = {
    "samsung": ("0303", "삼성카드"),
    "hyundai": ("0302", "현대카드"),
    "kb": ("0301", "KB국민카드"),
    "shinhan": ("0306", "신한카드"),
    "lotte": ("0311", "롯데카드"),
    "woori": ("0309", "우리카드"),
}


class CardCheckError(Exception):
    """원문 메시지·요청 내용 대신 안전한 안내와 검증된 오류 코드만 출력합니다."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


def encrypt_password(public, password):
    """공급자 RSA 공개키로 암호화합니다. 암호화 실패 시 입력값을 노출하지 않습니다."""
    public = validate_public_key(public)
    if public.startswith("-----BEGIN"):
        key = serialization.load_pem_public_key(public.encode("ascii"))
    else:
        key = serialization.load_der_public_key(base64.b64decode("".join(public.split())))
    try:
        value = password.encode("utf-8")
        if not value or len(value) > key.key_size // 8 - 11:
            raise ValueError()
        return base64.b64encode(key.encrypt(value, padding.PKCS1v15())).decode("ascii")
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise CardCheckError("카드사 비밀번호 길이와 CODEF 공개키 형식을 확인해 주세요.") from None


def validate_dates(start, end):
    """첫 시험은 정확한 날짜로 최대 7일만 조회합니다. 긴 기간 분할은 추후 작업입니다."""
    try:
        if not all(isinstance(v, str) and re.fullmatch(r"[0-9]{8}", v) for v in (start, end)):
            raise ValueError()
        first = datetime.strptime(start, "%Y%m%d").date()
        last = datetime.strptime(end, "%Y%m%d").date()
        if not 0 <= (last - first).days <= 6:
            raise ValueError()
    except (ValueError, TypeError):
        raise CardCheckError("조회 기간은 YYYYMMDD 형식의 최대 7일이어야 합니다.") from None


def kb_payload(settings, login_id, password, start, end):
    """기존 KB 시험 호출을 유지합니다. 소지 확인 자동 처리는 저장을 고른 연결의 작업자만 합니다(codef_sync.collect)."""
    return approval_payload(settings, "kb", login_id, password, start, end)


def validate_lotte_name(value):
    """붙여넣기 경계의 공백·BOM·제로폭 공백만 제거하고 한글 조합을 NFC로 맞춥니다.

    이름 내부의 성·이름 순서와 공백은 유지합니다. 내부 제어 문자를 지워 임의의 다른
    이름을 만들지 않으며, 오류 안내에 실제 이름이나 입력 조각을 포함하지 않습니다.
    """
    if not isinstance(value, str):
        raise CardCheckError("본인 이름을 텍스트로 입력해 주세요.")
    value = re.sub(r"^[\s\ufeff\u200b]+|[\s\ufeff\u200b]+$", "", value)
    value = unicodedata.normalize("NFC", value)
    if not value:
        raise CardCheckError(
            "이름이 비어 있습니다. 등록된 본인 이름을 복사해 한 번에 붙여 넣어 주세요."
        )
    if not 1 <= len(value) <= 80 or not value.isprintable() or not any(c.isalpha() for c in value):
        raise CardCheckError(
            "이름 형식을 읽지 못했습니다. 등록된 이름만 복사해 한 번에 붙여 넣어 주세요."
        )
    return value


def approval_payload(
    settings,
    card,
    login_id,
    password,
    start,
    end,
    *,
    card_number="",
    card_pin="",
    user_name="",
    identity="",
    phone_no="",
    telecom="",
):
    """선택 카드사에 직접 로그인합니다. 롯데는 이름·identity 7자리·휴대폰·통신사를 추가합니다.

    현대는 ID 로그인에도 loginCardNo와 RSA 암호화한 카드 비밀번호 4자리가 필요합니다.
    가맹점 상세 옵션은 0으로 유지해 현대의 조회 의미가 매입 내역으로 바뀌지 않게 합니다.
    """
    if card not in CARD_OPTIONS:
        raise CardCheckError("지원하지 않는 시험 카드사입니다. 안내된 --card 이름을 확인해 주세요.")
    organization, label = CARD_OPTIONS[card]
    validate_dates(start, end)
    if (
        not isinstance(login_id, str)
        or not 1 <= len(login_id) <= 256
        or any(c.isspace() or not c.isprintable() for c in login_id)
    ):
        raise CardCheckError(f"{label} 홈페이지 로그인 아이디 형식을 확인해 주세요.")
    payload = dict(
        organization=organization,
        loginType="1",
        id=login_id,
        password=encrypt_password(settings["public_key"], password),
        startDate=start,
        endDate=end,
        orderBy="0",
        inquiryType="1",
        memberStoreInfoType="0",
    )
    if card == "hyundai":
        if not isinstance(card_number, str) or not re.fullmatch(r"[0-9]{14,19}", card_number):
            raise CardCheckError("현대카드 전체 카드번호를 숫자만 입력해 주세요.")
        if not isinstance(card_pin, str) or not re.fullmatch(r"[0-9]{4}", card_pin):
            raise CardCheckError("현대카드 비밀번호는 숫자 4자리여야 합니다.")
        payload["loginCardNo"] = card_number
        # 현대는 승인 조회의 소지 확인에서도 cardNo를 요구할 수 있습니다. 전체 조회 옵션은 유지합니다.
        payload["cardNo"] = card_number
        payload["cardPassword"] = encrypt_password(settings["public_key"], card_pin)
    elif card_number or card_pin:
        raise CardCheckError("현대 이외의 시험에서는 추가 카드번호·PIN을 전송하지 않습니다.")
    if card == "lotte":
        payload["userName"] = validate_lotte_name(user_name)
        # 전체 주민번호를 잘라 쓰지 않습니다. 제공 명세의 7자리 외 입력은 전송 전에 중단합니다.
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9]{7}", identity):
            raise CardCheckError(
                "롯데 본인 확인은 생년월일 6자리와 주민번호 뒷자리 첫 숫자를 합친 7자리만 입력하세요. "
                "전체 주민번호는 입력하지 마세요."
            )
        payload["identity"] = identity
        if not isinstance(phone_no, str):
            raise CardCheckError("본인 명의 휴대폰번호를 입력해 주세요.")
        phone_no = phone_no.replace("-", "").replace(" ", "")
        if not re.fullmatch(r"01[016789][0-9]{7,8}", phone_no):
            raise CardCheckError(
                "본인 명의 휴대폰번호 형식을 확인해 주세요. 번호는 표시하지 않습니다."
            )
        if telecom not in ("0", "1", "2", "3", "4", "5"):
            raise CardCheckError("통신사는 안내된 숫자 0~5 중 하나를 입력해 주세요.")
        payload["phoneNo"] = phone_no
        payload["telecom"] = telecom
    elif user_name or identity or phone_no or telecom != "":
        raise CardCheckError(
            "롯데 이외의 시험에서는 이름·본인 확인·휴대폰 정보를 전송하지 않습니다."
        )
    return payload


def post_codef(token, payload, operation="approval"):
    """URL 인코딩 JSON을 HTTPS로 전송합니다. 이동·재시도·원문 오류 출력은 하지 않습니다."""
    body = urllib.parse.quote(json.dumps(payload, ensure_ascii=False), safe="").encode("ascii")
    paths = {
        "approval": "/v1/kr/card/p/account/approval-list",
        "register": "/v1/account/create",
        "bank_transactions": "/v1/kr/bank/p/account/transaction-list",
    }
    if operation not in paths:
        raise CardCheckError("허용되지 않은 CODEF 작업입니다.")
    request = urllib.request.Request(
        DEMO_HOST + paths[operation],
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect()
    )
    try:
        # 카드사 조회는 OAuth보다 오래 걸립니다. CLI 또는 웹의 배경 인증 스레드에서 기다립니다.
        with opener.open(request, timeout=300) as response:
            if response.status != 200:
                raise CardCheckError("CODEF 승인 조회 HTTP 응답을 확인하지 못했습니다.")
            content = response.read(BODY_LIMIT + 1)
        if len(content) > BODY_LIMIT:
            raise ValueError()
        text = content.decode("utf-8").strip()
        if not text.startswith("{"):
            text = urllib.parse.unquote_plus(text, errors="strict")
        result = json.loads(text)
        if not isinstance(result, dict) or not isinstance(result.get("result"), dict):
            raise ValueError()
        return result
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if status in (401, 403):
            raise CardCheckError(
                "CODEF 데모 인증 또는 승인내역 상품 이용 권한을 확인해 주세요."
            ) from None
        if status == 429:
            raise CardCheckError("CODEF 요청 한도에 도달했습니다. 반복 실행하지 마세요.") from None
        raise CardCheckError("CODEF 승인 조회 서버가 요청을 처리하지 못했습니다.") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise CardCheckError(
            "조회 연결을 완료하지 못했습니다. 자동 재시도하지 않았습니다."
        ) from None
    except (ValueError, TypeError, UnicodeError):
        raise CardCheckError("CODEF 승인 조회 응답 형식을 확인하지 못했습니다.") from None


def failure_hint(result):
    """오류 원문을 메모리에서 분류하고 허용한 고정 안내만 반환합니다.

    기관 오류에는 이름·아이디·전화번호 등이 섞일 수 있어 일부 문장도 그대로 출력하지
    않습니다. 키워드 분류는 원인 확정이 아니라 공급자가 보낸 안내의 주제만 뜻합니다.
    공식 코드 의미와 공급자 원문에서 분류한 주제를 구분하며 비밀번호 오류로 확대 해석하지 않습니다.
    """
    # 공식 오류표에서 확인한 의미만 고정 안내로 제공합니다. 원문을 잘라 표시하지 않습니다.
    code = result.get("code")
    known_code = code if isinstance(code, str) else ""
    official = {
        "CF-12800": "공식 코드 안내: 아이디 오류 응답입니다. 카드사 홈페이지 아이디를 확인해 주세요. ",
        "CF-12831": "공식 코드 안내: 본인 인증 후 이용할 수 있습니다. 카드사 홈페이지의 본인 인증 안내를 확인해 주세요. ",
    }.get(known_code, "")
    messages = [result.get(name) for name in ("message", "extraMessage")]
    text = " ".join(value[:4096] for value in messages if isinstance(value, str))
    normalized = "".join(unicodedata.normalize("NFKC", text).lower().split())
    topics = (
        (("보안문자", "자동입력방지", "캡차", "captcha"), "보안문자·자동입력 방지"),
        (("비밀번호변경", "비밀번호재설정", "비밀번호만료"), "비밀번호 변경·만료"),
        (
            ("추가인증", "본인인증", "본인확인", "휴대폰인증", "sms인증", "ars인증", "기기인증"),
            "추가 본인 인증",
        ),
        (
            ("로그인제한", "접속제한", "이용제한", "계정잠금", "계정잠김", "아이디잠금", "차단"),
            "접속 제한·잠금",
        ),
        (
            ("아이디또는비밀번호", "아이디/비밀번호", "아이디와비밀번호", "로그인정보", "계정정보"),
            "로그인·계정 정보",
        ),
        (("서비스점검", "시스템점검", "일시적", "잠시후", "서비스장애"), "점검·일시적 장애"),
        (("이용권한", "상품권한", "미계약", "상품신청"), "상품 이용 권한"),
    )
    matched = [label for words, label in topics if any(word in normalized for word in words)]
    if matched:
        return official + "응답 안내 주제(원인 확정 아님): " + " / ".join(matched) + "."
    return official + "응답 안내 주제: 분류 불가. 이 코드의 상세 원인은 CODEF 확인이 필요합니다."


def missing_parameter_hint(result):
    """필수값 누락 응답의 알려진 키/문서상 기호만 표시합니다. 값·원문은 반환하지 않습니다."""
    allowed = (
        "loginType",
        "id",
        "password",
        "connectedId",
        "birthDate",
        "userName",
        "identity",
        "phoneNo",
        "telecom",
        "commSession",
        "loginCardNo",
        "cardNo",
        "cardPassword",
        "certType",
        "certFile",
        "keyFile",
        "certPassword",
        "account",
        "accountPassword",
        "withdrawAccountNo",
        "withdrawAccountPassword",
        "derFile",
        "reqTimeOut",
    )
    text = " ".join(
        result[key][:4096]
        for key in ("message", "extraMessage")
        if isinstance(result.get(key), str)
    )
    keys = [
        key for key in allowed if re.search(r"(?<![A-Za-z0-9_])" + key + r"(?![A-Za-z0-9_])", text)
    ]
    if result.get("code") in ("CF-12401", "CF-12411"):
        # 은행 명세에 명시된 reqUserPass와 정확히 일치하는 기호만 변환합니다.
        # 로그인 오류 횟수나 알 수 없는 userError 값은 원문 그대로 출력하지 않습니다.
        for name in ("userError", "errorUser"):
            value = result.get(name)
            key = "password" if value == "reqUserPass" else value
            if isinstance(key, str) and key in allowed and key not in keys:
                keys.append(key)
    return "응답에 언급된 키: " + (", ".join(keys) if keys else "확인 불가") + "."


def post_approval(token, payload):
    """기존 수동 시험과 동일한 승인 조회 주소만 사용합니다."""
    return post_codef(token, payload)


def registration_failure_hint(response, organization):
    """단일 대상 기관의 상세 코드와 고정 안내만 반환합니다. 원문·값은 노출하지 않습니다.

    CF-04000은 전체 등록 실패이고, errorList의 CF-12401 등은 기관의 세부 결과입니다.
    여러 기관·부분 성공·다른 기관의 결과는 선택하지 않고 기존 포괄 안내만 유지합니다.
    이 안내를 근거로 로그인이나 카드 비밀번호를 자동 재시도하지 않습니다.
    """
    data = response.get("data")
    if organization not in {option[0] for option in CARD_OPTIONS.values()} or not isinstance(
        data, dict
    ):
        return ""
    errors = data.get("errorList")
    if not isinstance(errors, list) or len(errors) != 1 or data.get("successList"):
        return ""
    entry = errors[0]
    if not isinstance(entry, dict) or any(
        entry.get(key, default) != expected
        for key, default, expected in (
            ("organization", None, organization),
            ("countryCode", "KR", "KR"),
            ("businessType", "CD", "CD"),
            ("clientType", "P", "P"),
        )
    ):
        return ""
    code = entry.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"CF-[0-9]{5}", code) or code == "CF-00000":
        return ""
    hint = missing_parameter_hint(entry) if code == "CF-12401" else failure_hint(entry)
    return f" 기관 상세 코드: {code}. " + hint


def two_way_challenge(response, organization, *, business_type="CD"):
    """단일 기관의 명시적인 추가 인증만 추출합니다. 빠진 세션을 추측해 만들지 않습니다.

    상품 조회의 data와 단일 기관 등록의 errorList를 구분하고, 플래그가 실제 True이며
    같은 기관인 경우만 처리합니다. 연결 정보가 twoWayInfo에 담겼어도 네 필드만 사용합니다.
    """
    data = response.get("data")
    code = response["result"].get("code")
    outer = data
    if (code == "CF-03002" or (code == "CF-04034" and organization == "0311")) and isinstance(
        data, dict
    ):
        source = data
    elif code == "CF-00000" and isinstance(data, dict):
        errors = data.get("errorList")
        if not isinstance(errors, list) or len(errors) != 1 or data.get("successList"):
            return None
        entry = errors[0]
        if (
            not isinstance(entry, dict)
            or entry.get("code") not in ("CF-03002", "CF-04034")
            or entry.get("organization") != organization
        ):
            return None
        if (
            entry.get("businessType", business_type) != business_type
            or entry.get("clientType", "P") != "P"
            or entry.get("countryCode", "KR") != "KR"
        ):
            return None
        code = entry["code"]
        if code == "CF-04034" and organization != "0311":
            return None
        source = entry.get("data", entry)
        outer = entry
        if not isinstance(source, dict):
            return None
    else:
        return None
    info = source.get("twoWayInfo", data.get("twoWayInfo", source))
    if not isinstance(info, dict):
        raise CardCheckError(
            f"추가 인증 연결 정보 형식을 확인하지 못했습니다. [코드: {code}]", code=code
        )
    flag = source.get(
        "continue2Way",
        info.get("continue2Way", outer.get("continue2Way", data.get("continue2Way"))),
    )
    if flag is not True:
        if code == "CF-04034":
            raise CardCheckError(
                "롯데 추가 인증 안내를 받았지만 인증 재개 플래그 continue2Way가 True가 아니어서 자동 진행하지 않았습니다. [코드: CF-04034]",
                code=code,
            )
        return None
    fields = ("jobIndex", "threadIndex", "jti", "twoWayTimestamp")
    if any(info.get(name) is None for name in fields):
        missing = [name for name in fields if info.get(name) is None]
        raise CardCheckError(
            "문자 수신과 별개로 인증 재개 정보가 누락돼 전송하지 않았습니다. 누락 키: "
            + ", ".join(missing)
            + f" [코드: {code}]",
            code=code,
        )
    if any(
        name in container and container[name] != info[name]
        for container in (source, outer, data)
        for name in fields
    ):
        raise CardCheckError(
            f"추가 인증 연결 정보가 서로 달라 전송하지 않았습니다. [코드: {code}]", code=code
        )
    methods = [
        container.get("method")
        for container in (source, info, outer, data)
        if container.get("method") not in (None, "")
    ]
    if methods and any(method != methods[0] for method in methods):
        raise CardCheckError(
            f"추가 인증 방식 안내가 서로 달라 전송하지 않았습니다. [코드: {code}]", code=code
        )
    return {
        "code": code,
        **{name: info[name] for name in fields},
        "method": methods[0] if methods else None,
        "commSession": source.get("commSession", outer.get("commSession", data.get("commSession"))),
    }


def query_approvals(token, payload, answer_challenge, *, sender=None, organization=None):
    """추가 인증 응답만 최대 3회 이어갑니다. 로그인 오류는 재시도하지 않습니다."""
    request = dict(payload)
    send = sender or post_approval
    organization = organization or payload.get("organization")
    for attempt in range(MAX_CHALLENGES + 1):
        response = send(token, request)
        code = response["result"].get("code")
        data = response.get("data")
        challenge = two_way_challenge(response, organization)
        if code == "CF-00000" and challenge is None:
            return data
        if challenge is not None:
            if attempt == MAX_CHALLENGES:
                raise CardCheckError(
                    "추가 인증 횟수를 초과했습니다. 자동 재시도하지 않았습니다.",
                    code=challenge["code"],
                )
            method = challenge.get("method")
            # 롯데의 문서상 SMS 응답은 method 없이 연결 정보만 올 수 있습니다.
            # 기관·오류 코드·명시적 continue2Way를 확인한 이 지점에서만 SMS로 해석합니다.
            # 명시된 다른 방식이나 다른 기관을 임의로 SMS 인증으로 바꾸지 않습니다.
            if organization == "0311" and method in (None, ""):
                method = "smsAuthNo"
            if method not in ("simpleAuth", "smsAuthNo", "emailAuthNo"):
                raise CardCheckError(
                    f"이 시험 도구가 지원하지 않는 추가 인증입니다. [코드: {challenge['code']}]",
                    code=challenge["code"],
                )
            fields = ("jobIndex", "threadIndex", "jti", "twoWayTimestamp")
            session = challenge.get("commSession") if organization == "0311" else None
            if session is None and organization == "0311":
                session = request.get("commSession")
            if session is not None and (not isinstance(session, str) or len(session) > 65536):
                raise CardCheckError("롯데 인증 세션 형식을 확인하지 못했습니다.")
            answer = answer_challenge(method)
            pattern = r"[0-9]{1,32}" if method == "smsAuthNo" else r"[A-Za-z0-9]{1,32}"
            if (
                not isinstance(answer, str)
                or not re.fullmatch(pattern, answer)
                or (method == "simpleAuth" and answer != "1")
            ):
                raise CardCheckError("추가 인증 입력 형식을 확인해 주세요. 재전송하지 않았습니다.")
            request = {
                **payload,
                method: answer,
                "is2Way": True,
                "twoWayInfo": {name: challenge[name] for name in fields},
            }
            if session:
                request["commSession"] = session
            continue
        safe_code = (
            code if isinstance(code, str) and re.fullmatch(r"CF-[0-9]{5}", code) else "확인 불가"
        )
        if code == "CF-04000":
            raise CardCheckError(
                "사용자 계정정보 등록에 실패했습니다. 비밀번호 오류로 단정하지 않으며 자동 재시도하지 않았습니다. "
                "[코드: CF-04000]" + registration_failure_hint(response, organization),
                code=code,
            )
        if code == "CF-04034" and organization == "0311":
            raise CardCheckError(
                "롯데 추가 인증 안내를 받았지만 인증을 이어갈 연결 정보를 확인하지 못했습니다. 로그인 정보를 다시 입력하는 오류로 단정하지 않습니다. [코드: CF-04034]",
                code=code,
            )
        if code == "CF-12108":
            raise CardCheckError(
                "KB 카드 소지 확인이 필요합니다. 더보기 → 카드·계좌 연결에서 KB를 다시 연결하며 카드번호와 비밀번호 앞 2자리를 "
                "입력하면 저장해 다음부터 자동으로 처리합니다. "
                "[코드: CF-12108]",
                code="CF-12108",
            )
        if code == "CF-12401":
            raise CardCheckError(
                "로그인 파라미터 누락 응답입니다. 자동 재시도하지 않았습니다. [코드: CF-12401] "
                + missing_parameter_hint(response["result"]),
                code="CF-12401",
            )
        if code == "CF-12834":
            raise CardCheckError(
                "카드 비밀번호 오류로 이용이 제한됐습니다. 재시도하지 말고 카드사에서 확인해 주세요. "
                "[코드: CF-12834]",
                code="CF-12834",
            )
        if code == "CF-12851":
            raise CardCheckError(
                "사용자 이름이 누락됐다는 응답입니다. 롯데 시험 도구를 업데이트하고 본인 이름을 입력해 주세요. "
                "[코드: CF-12851]",
                code="CF-12851",
            )
        if code == "CF-12850":
            raise CardCheckError(
                "본인 확인 정보가 누락됐다는 응답입니다. 롯데 시험 도구를 업데이트하고 identity 7자리를 입력해 주세요. "
                "[코드: CF-12850]",
                code="CF-12850",
            )
        raise CardCheckError(
            f"카드사 조회를 완료하지 못했습니다. 자동 재시도하지 않았습니다. [코드: {safe_code}] "
            + failure_hint(response["result"]),
            code=code if isinstance(code, str) and re.fullmatch(r"CF-[0-9]{5}", code) else None,
        )


def summarize(data):
    """실제 거래 필드로 건수만 검증합니다. 원문·가맹점·카드번호·금액은 반환하지 않습니다.

    부분취소·음수 등은 임의로 순지출에 합산하지 않습니다. API 성공과 거래 해석 성공을
    구분하며, 0건도 유효한 결과입니다. 요청 범위의 완전 수집을 이 요약만으로 보증하지 않습니다.
    """
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict) and "resUsedDate" in data:
        rows = [data]
    else:
        raise CardCheckError("승인 응답의 목록 구조를 확인해야 합니다. 원문은 저장하지 않았습니다.")
    counts = Counter(total=len(rows), approval=0, cancel=0, partial=0, declined=0, invalid=0)
    statuses = {"0": "approval", "1": "cancel", "2": "partial", "3": "declined"}
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ValueError()
            date, time = row.get("resUsedDate"), row.get("resUsedTime")
            if not isinstance(date, str) or not re.fullmatch(r"[0-9]{8}", date):
                raise ValueError()
            if not isinstance(time, str) or not re.fullmatch(r"[0-9]{6}", time):
                raise ValueError()
            datetime.strptime(date + time, "%Y%m%d%H%M%S")
            amount = row.get("resUsedAmount")
            if not isinstance(amount, str) or not re.fullmatch(
                r"-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", amount
            ):
                raise ValueError()
            status = statuses.get(row.get("resCancelYN"))
            if not status:
                raise ValueError()
            counts[status] += 1
        except (ValueError, TypeError):
            counts["invalid"] += 1
    return dict(counts)
