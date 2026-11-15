"""가맹점 이름을 소비 분야로 분류하고, 간편결제 이름과 실제 소비처를 구분합니다.

직접 정한 분류가 자동 추정보다 우선합니다. 네이버페이 같은 결제 수단 이름에는
가맹점 공통 분류를 적용하지 않아 서로 다른 소비처가 한 분야로 묶이는 것을 막습니다.
"""

import re
import unicodedata
import uuid

CATEGORIES = [
    "식비",
    "커피",
    "의료",
    "마트·식료품",
    "편의점",
    "교통",
    "차량·주유",
    "쇼핑",
    "문화·여가",
    "교육",
    "육아",
    "구독료",
    "통신·공과금",
    "주거·생활",
    "여행·숙박",
    "보험·금융",
    "기타",
    "미분류",
]


def merchant_key(name):
    """공백과 문자 표현 차이를 정리해 같은 가맹점 이름을 비교할 키를 만듭니다."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name)).casefold()


PAYMENT_SERVICES = [
    (
        "네이버페이",
        ("네이버페이", "네이버파이낸셜"),
        r"(?:naver[\s_-]*(?:pay|financial)|n[\s_-]*pay)",
    ),
    ("카카오페이", ("카카오페이",), r"kakao[\s_-]*pay"),
    ("토스페이", ("토스페이먼츠", "토스페이"), r"toss[\s_-]*(?:payments|pay)"),
    ("페이코", ("페이코",), r"payco"),
    ("삼성페이", ("삼성페이", "삼성월렛"), r"samsung[\s_-]*(?:pay|wallet)"),
    ("애플페이", ("애플페이",), r"apple[\s_-]*pay"),
    ("구글페이", ("구글페이",), r"google[\s_-]*pay"),
    ("SSG페이", ("쓱페이", "ssg페이"), r"ssg[\s_-]*pay"),
    ("스마일페이", ("스마일페이",), r"smile[\s_-]*pay"),
    ("쿠페이", ("쿠페이",), r"coupay"),
    ("엘페이", ("엘페이",), r"l[\s._-]*pay"),
    ("KB페이", ("kb페이",), r"kb[\s_-]*pay"),
    (
        "신한페이",
        ("신한플레이", "신한쏠페이", "신한sol페이"),
        r"shinhan[\s_-]*(?:play|(?:sol[\s_-]*)?pay)",
    ),
    ("하나페이", ("하나페이", "하나원큐페이"), r"hana[\s_-]*pay"),
    ("우리WON페이", ("우리won페이", "우리원페이"), r"(?:woori[\s_-]*)?won[\s_-]*pay"),
    ("페이북", ("페이북",), r"paybooc"),
    ("제로페이", ("제로페이",), r"zero[\s_-]*pay"),
    ("알리페이", ("알리페이",), r"ali[\s_-]*pay"),
    ("위챗페이", ("위챗페이",), r"wechat[\s_-]*pay"),
    ("페이팔", ("페이팔",), r"paypal"),
    ("아마존페이", ("아마존페이",), r"amazon[\s_-]*pay"),
    ("NHN KCP", ("엔에이치엔케이씨피", "한국사이버결제", "nhn케이씨피"), r"(?:nhn[\s_-]*)?kcp"),
    ("KG이니시스", ("이니시스",), r"(?:kg[\s_-]*)?inicis"),
    ("KG모빌리언스", ("모빌리언스",), r"(?:kg[\s_-]*)?mobilians"),
    ("나이스페이", ("나이스페이", "나이스정보통신"), r"nice[\s_-]*(?:pay|payments)"),
    ("다날", ("다날",), r"danal"),
    ("KSNET", ("케이에스넷",), r"ks[\s_-]*net"),
    ("페이레터", ("페이레터",), r"pay[\s_-]*letter"),
    ("카페24", ("카페24",), r"cafe[\s_-]*24"),
]


def payment_service(name):
    """가맹점 이름에 알려진 간편결제·결제대행사가 있으면 표준 이름을 반환합니다."""
    text = unicodedata.normalize("NFKC", name).casefold()
    key = merchant_key(name)
    for label, tokens, latin in PAYMENT_SERVICES:
        if any(token in key for token in tokens) or re.search(
            r"(?<![a-z0-9])(?:" + latin + r")(?![a-z])", text
        ):
            return label
    return None


def schema(db):
    """기존 거래 보충 정보와 계정별 분야 이름을 준비합니다. 기존 분류 키는 바꾸지 않습니다."""
    db.executescript("""CREATE TABLE IF NOT EXISTS transaction_annotations(
        transaction_id INTEGER PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
        category TEXT,actual_merchant TEXT NOT NULL DEFAULT '',memo TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS spending_categories(
        owner_id INTEGER NOT NULL REFERENCES users(id),category TEXT NOT NULL,
        name TEXT NOT NULL,custom INTEGER NOT NULL DEFAULT 0 CHECK(custom IN (0,1)),
        PRIMARY KEY(owner_id,category));""")
    # 최초 도입 때의 기존 거래는 확인 상태로 보존하고 이후 등록분부터 안내합니다.
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='category_reviews'"
    ).fetchone():
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='category_reviews'"
            ).fetchone():
                db.execute(
                    "CREATE TABLE category_reviews(transaction_id INTEGER PRIMARY KEY "
                    "REFERENCES transactions(id) ON DELETE CASCADE,checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                db.execute(
                    "INSERT INTO category_reviews(transaction_id) SELECT id FROM transactions"
                )


def category_review_summary(db, owner):
    """신규 등록 후 아직 분야를 확인하지 않은 본인 집계 대상 거래 수만 반환합니다."""
    count = db.execute(
        "SELECT COUNT(*) FROM transactions t JOIN imports i ON i.id=t.import_id "
        "LEFT JOIN category_reviews r ON r.transaction_id=t.id "
        "LEFT JOIN transaction_exclusions e ON e.transaction_id=t.id "
        "WHERE i.owner_id=? AND r.transaction_id IS NULL AND e.transaction_id IS NULL",
        (owner,),
    ).fetchone()[0]
    from bank_expenses import review_summary

    return dict(pending_count=count, bank_review_summary=review_summary(db, owner))


def category_catalog(db, owner):
    """기본 분야와 본인이 추가한 분야를 합칩니다. 이름을 바꿔도 분류 키와 자동 규칙은 유지합니다."""
    settings = {
        row["category"]: dict(row)
        for row in db.execute(
            "SELECT category,name,custom FROM spending_categories WHERE owner_id=? ORDER BY rowid",
            (owner,),
        )
    }
    result = [
        dict(key=key, name=settings.get(key, {}).get("name", key), custom=False)
        for key in CATEGORIES
    ]
    result.extend(
        dict(key=key, name=row["name"], custom=True)
        for key, row in settings.items()
        if row["custom"] and key not in CATEGORIES
    )
    return result


def category_labels(db, owner):
    """자동·거래별·가맹점별 분류를 같은 개인 표시 이름으로 읽기 위한 사전입니다."""
    return {row["key"]: row["name"] for row in category_catalog(db, owner)}


def category_usage(db, owner):
    """현재 거래뿐 아니라 앞으로 적용할 가맹점 규칙에 쓰인 분야도 삭제하지 않도록 확인합니다."""
    return {
        row[0]
        for row in db.execute(
            "SELECT category FROM merchant_categories WHERE owner_id=? UNION "
            "SELECT a.category FROM transaction_annotations a JOIN transactions t "
            "ON t.id=a.transaction_id JOIN imports i ON i.id=t.import_id "
            "WHERE i.owner_id=? AND a.category IS NOT NULL",
            (owner, owner),
        )
    }


def category_report(db, owner):
    """개인 분야 관리에 필요한 목록·삭제 가능 여부만 반환합니다."""
    used = category_usage(db, owner)
    return dict(items=[dict(row, in_use=row["key"] in used) for row in category_catalog(db, owner)])


def save_category(db, owner, name, key=None):
    """이름 중복과 길이를 검사하고 개인 분야를 추가하거나 이름만 바꿉니다."""
    if not isinstance(name, str):
        raise ValueError("분야 이름을 입력해 주세요.")
    if len(name) > 200 or any(unicodedata.category(char).startswith("C") for char in name):
        raise ValueError("분야 이름은 제어 문자 없이 1~24자로 입력해 주세요.")
    name = " ".join(unicodedata.normalize("NFKC", name).strip().split())
    if (
        not 1 <= len(name) <= 24
        or name == "__auto__"
        or any(unicodedata.category(char).startswith("C") or char in "<>" for char in name)
    ):
        raise ValueError("분야 이름은 제어 문자나 꺾쇠 없이 1~24자로 입력해 주세요.")
    catalog = category_catalog(db, owner)
    current = next((row for row in catalog if row["key"] == key), None)
    if key is not None and current is None:
        raise LookupError("분야를 찾지 못했습니다.")
    if key == "미분류":
        raise ValueError("미분류는 기본 분류에 필요하여 이름을 바꿀 수 없습니다.")
    if any(
        merchant_key(row["name"]) == merchant_key(name) and row["key"] != key for row in catalog
    ):
        raise ValueError("이미 사용 중인 분야 이름입니다.")
    if key is None:
        if sum(row["custom"] for row in catalog) >= 30:
            raise ValueError("직접 추가하는 분야는 30개까지 사용할 수 있습니다.")
        key = "custom:" + uuid.uuid4().hex
    custom = key not in CATEGORIES
    db.execute(
        "INSERT INTO spending_categories(owner_id,category,name,custom) VALUES(?,?,?,?) "
        "ON CONFLICT(owner_id,category) DO UPDATE SET name=excluded.name",
        (owner, key, name, int(custom)),
    )


def delete_category(db, owner, key):
    """사용하지 않는 추가 분야만 삭제합니다. 거래·규칙과 기본 분야는 보존합니다."""
    row = next((row for row in category_catalog(db, owner) if row["key"] == key), None)
    if row is None:
        raise LookupError("분야를 찾지 못했습니다.")
    if not row["custom"]:
        raise ValueError("기본 분야는 이름을 바꿔 사용할 수 있으며 삭제할 수 없습니다.")
    if key in category_usage(db, owner):
        raise ValueError("거래나 가맹점 분류에 사용 중입니다. 다른 분야로 변경한 뒤 삭제해 주세요.")
    db.execute("DELETE FROM spending_categories WHERE owner_id=? AND category=?", (owner, key))


# Specific delivery/cafe brands precede broad marketplace and food hints.
RULES = [
    ("육아", ("어린이집", "유치원", "산후조리원", "키즈카페", "아가방")),
    (
        "구독료",
        (
            "넷플릭스",
            "netflix",
            "유튜브프리미엄",
            "디즈니플러스",
            "스포티파이",
            "spotify",
            "왓챠",
            "티빙",
        ),
    ),
    ("식비", ("쿠팡이츠", "배달의민족", "배민", "요기요")),
    (
        "커피",
        (
            "스타벅스",
            "이디야",
            "투썸",
            "메가엠지씨",
            "메가커피",
            "컴포즈커피",
            "빽다방",
            "폴바셋",
            "할리스",
            "커피빈",
            "엔제리너스",
            "카페",
            "커피",
            "cafe",
            "coffee",
        ),
    ),
    ("의료", ("병원", "의원", "약국", "치과", "한의원", "의료재단", "의료법인")),
    ("편의점", ("gs25", "씨유", "세븐일레븐", "이마트24", "미니스톱")),
    (
        "식비",
        (
            "식당",
            "음식점",
            "국밥",
            "김밥",
            "분식",
            "치킨",
            "피자",
            "햄버거",
            "맥도날드",
            "롯데리아",
            "버거킹",
            "써브웨이",
            "한솥",
            "파리바게뜨",
            "뚜레쥬르",
            "베이커리",
        ),
    ),
    ("차량·주유", ("주유소", "충전소", "현대오일뱅크", "에쓰오일", "자동차정비", "세차장")),
    (
        "마트·식료품",
        ("이마트", "홈플러스", "롯데마트", "코스트코", "하나로마트", "슈퍼마켓", "식자재마트"),
    ),
    (
        "교통",
        (
            "택시",
            "카카오t",
            "코레일",
            "철도",
            "고속버스",
            "시외버스",
            "도시철도",
            "지하철",
            "후불교통",
            "하이패스",
            "주차장",
        ),
    ),
    (
        "여행·숙박",
        (
            "호텔",
            "리조트",
            "대한항공",
            "아시아나항공",
            "제주항공",
            "진에어",
            "티웨이",
            "에어부산",
            "야놀자",
            "여기어때",
        ),
    ),
    (
        "문화·여가",
        (
            "cgv",
            "메가박스",
            "롯데시네마",
            "교보문고",
            "영풍문고",
            "알라딘",
            "yes24",
            "영화관",
            "볼링장",
            "헬스장",
        ),
    ),
    ("교육", ("학원", "인터넷강의", "평생교육", "수강료")),
    (
        "통신·공과금",
        (
            "sk텔레콤",
            "skt통신",
            "kt통신",
            "lg유플러스",
            "한국전력",
            "도시가스",
            "통신요금",
            "휴대폰요금",
            "상하수도",
        ),
    ),
    (
        "보험·금융",
        ("보험료", "삼성생명", "삼성화재", "현대해상", "kb손해보험", "한화생명", "교보생명"),
    ),
    (
        "쇼핑",
        ("쿠팡", "무신사", "올리브영", "다이소", "백화점", "11번가", "지마켓", "g마켓", "옥션"),
    ),
    ("주거·생활", ("관리비", "세탁소", "미용실", "헤어샵")),
]


def classify(name, overrides):
    """공통 직접 분류를 먼저 확인하고, 없으면 구체적인 가맹점 단서부터 자동 분류합니다."""
    key = merchant_key(name)
    if not payment_service(name) and key in overrides:
        return overrides[key], "manual"
    key = key.replace("카페24", "").replace("cafe24", "")
    if re.match(r"^cu(?:$|[가-힣0-9_(])", key):
        return "편의점", "rule"
    if key.startswith(("카페24", "cafe24")):
        return "미분류", "unclassified"
    for category, hints in RULES:
        if any(hint in key for hint in hints):
            return category, "rule"
    return "미분류", "unclassified"
