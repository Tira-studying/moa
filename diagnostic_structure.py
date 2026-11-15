"""명세서 판독 오류를 설명하기 위해 개인정보를 가상값으로 바꾸는 변환기입니다.

실제 텍스트·주소·입력값·스크립트를 전달하지 않고 제한된 HTML 구조와 진단용 건수만 남깁니다.
만든 구조는 오류 분석용이며, 원본 명세서를 대신하거나 실행하는 용도가 아닙니다.
"""

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

LABELS = set(
    "이용일 이용일자 사용일 사용일자 승인일 승인일자 거래일 거래일자 매출일 매출일자 날짜 가맹점 가맹점명 이용가맹점 이용처 사용처 상호 업체명 이용금액 사용금액 승인금액 청구금액 금액 원금 할부금액 결제금액 이번달원금 이번달청구금액 할부원금 청구원금 할부수수료 수수료 이자액 이자 이용원금 청구잔액 잔액 원 KRW 본인 가족 국내 해외 일시불 할부 정상 승인 취소 승인취소 결제취소 환불 취소완료 환불완료 부분취소 부분환불 카드번호 소계 총 총계 합계 상세보기 자세히 접기 펼치기 더보기 이용내역더보기 거래내역더보기 삼성카드".split()
)
HINTS = (
    "merchant",
    "store",
    "shop",
    "name",
    "amount",
    "price",
    "money",
    "item",
    "record",
    "transaction",
    "date",
    "btn_more",
    "more_btn",
    "tit",
)
VOID = {"br", "hr", "img", "input", "wbr", "col"}
BLOCK = {
    "head",
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "svg",
    "noscript",
    "template",
    "textarea",
}


def fictional_text(value):
    """진단에 필요한 공개 UI 문구·작은 건수만 남기고 날짜·금액·일반 문자를 가상화합니다."""
    compact = re.sub(r"[\s\u200b\ufeff]", "", value)
    # These are UI counts, never account identifiers or financial amounts.
    count_label = re.fullmatch(r"(?:소계)?(?:총)?[\d,]+건", compact) or re.fullmatch(
        r"(?:이용내역|거래내역)?더보기[ (（]*\d*(?:[/／]\d+)?(?:건)?[)）]*", compact
    )
    safe_counts = all(
        len(n) <= 5 and int(n) <= 20000 for n in re.findall(r"\d+", compact.replace(",", ""))
    )
    if compact in LABELS or (count_label and safe_counts):
        return value
    if re.fullmatch(r"[\s\d,.(+\-)/₩원KRW]+", value):
        return re.sub(r"\d", "1", value)
    prefix = re.match(
        r"^(\s*(?:본인|가족|이용일자?|사용일자?|승인일자?|이용금액|청구금액|원금|할부수수료)\s*[:：]?)",
        value,
    )
    start = prefix.end() if prefix else 0
    rest = "".join(
        (
            "1"
            if c.isdecimal()
            else (
                "x"
                if c.isalpha()
                or (not c.isascii() and not c.isspace() and c not in "●·…₩：＋－（）／．")
                else c
            )
        )
        for c in value[start:]
    )
    return value[:start] + rest


class StructureParser(HTMLParser):
    """HTML을 실행하지 않고 읽으며 주소·스크립트·입력값을 제거한 구조를 만듭니다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.in_body = False
        self.blocked = []
        self.nodes = 0
        self.size = 0
        self.truncated = False

    def append(self, value):
        if self.truncated:
            return
        if self.size + len(value) > 500000:
            self.truncated = True
            return
        self.size += len(value)
        self.parts.append(value)

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.in_body = True
        if tag in BLOCK or self.blocked:
            if tag not in VOID and tag not in {"meta", "link", "source"}:
                self.blocked.append(tag)
            return
        if not self.in_body or tag in {"html", "body"}:
            return
        self.nodes += 1
        if self.nodes > 12000:
            self.truncated = True
            return
        safe = []
        for key, value in attrs:
            if key in {"hidden", "disabled"}:
                safe.append((key, key))
                continue
            if key == "onclick":
                safe.append(("data-click", "1"))
                continue
            if value is None:
                continue
            if key == "href":
                kind = (
                    "fragment"
                    if value.startswith("#")
                    else (
                        "script"
                        if value.lower().startswith("javascript:")
                        else "relative" if not urlsplit(value).scheme else "external"
                    )
                )
                host = urlsplit(value).hostname or ""
                if host == "samsungcard.com" or host.endswith(".samsungcard.com"):
                    kind = "official"
                safe.append(("data-link-kind", kind))
                continue
            if key == "src" and tag in {"img", "input"}:
                if "more" in urlsplit(value).path.rsplit("/", 1)[-1].lower():
                    safe.append(("data-image-hint", "more"))
                continue
            if key in {"rowspan", "colspan"} and value.isdigit() and 1 <= int(value) <= 100:
                safe.append((key, value))
            elif key == "class":
                hints = [
                    hint
                    for hint in HINTS
                    if hint in value.lower()
                    and (
                        hint not in {"item", "record", "transaction"}
                        or re.search(r"(?:^|[\s_-])" + hint + r"(?:$|[\s_-])", value.lower())
                    )
                ]
                if hints:
                    safe.append(("class", " ".join(hints)))
            elif key in {"alt", "title", "aria-label"}:
                safe.append((key, fictional_text(value[:300])))
            elif key == "role" and value in {"button", "listitem", "list", "row", "cell"}:
                safe.append((key, value))
            elif key in {"aria-expanded", "aria-disabled"} and value in {"true", "false"}:
                safe.append((key, value))
            elif (
                key == "type"
                and tag == "input"
                and value in {"button", "submit", "image", "password", "hidden", "text"}
            ):
                safe.append((key, value))
            elif key == "data-moa-hidden" and value == "1":
                safe.append(("hidden", "hidden"))
            elif key in {"hidden", "disabled"}:
                safe.append((key, key))
            elif key == "style" and (
                re.search(r"display\s*:\s*none", value, re.I)
                or re.search(r"visibility\s*:\s*hidden", value, re.I)
            ):
                safe.append(("hidden", "hidden"))
        # All identifiers, URLs, values, handlers, CSS and data attributes are gone.
        encoded = "".join(
            " " + key + '="' + html.escape(value, quote=True) + '"' for key, value in safe
        )
        self.append("<" + tag + encoded + ">")

    def handle_endtag(self, tag):
        if self.blocked:
            if tag in self.blocked:
                while self.blocked:
                    if self.blocked.pop() == tag:
                        break
            return
        if tag == "body":
            self.in_body = False
            return
        if self.in_body and tag not in VOID and re.fullmatch("[a-z0-9-]+", tag):
            self.append("</" + tag + ">")

    def handle_data(self, value):
        if self.in_body and not self.blocked:
            self.append(html.escape(fictional_text(value)))


def sanitize_structure(raw):
    """가상 HTML 구조와 제한된 진단 정보를 하나의 다운로드용 결과로 만듭니다."""
    if not isinstance(raw, str) or not raw or len(raw) > 2 * 1024 * 1024:
        return None
    try:
        parser = StructureParser()
        parser.feed(raw)
        parser.close()
        if not parser.nodes:
            return None
        return {
            "schema": 1,
            "fictional": True,
            "truncated": parser.truncated,
            "html": "<!doctype html><html><body>" + "".join(parser.parts) + "</body></html>",
        }
    except Exception:
        return None
