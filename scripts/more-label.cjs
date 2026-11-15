/**
 * 더보기 글자·아이콘에 쓰는 공통 정규식입니다.
 * 정규식은 글자 모양의 규칙이며, 적용 전 공백과 유니코드 표현을 정리합니다.
 */

// Normalize whitespace/NFKC before applying this pattern.
const MORE_LABEL_PATTERN =
  '^(?:더보기|이용내역더보기|거래내역더보기)(?:[(]?[0-9,]+(?:[/][0-9,]+)?(?:건)?[)]?)?$';
const MORE_ICON_PATTERN = '(?:^|[/_-])(?:btn[_-]?)?more(?:[_-]btn)?[.](?:gif|png|svg|webp)$';
module.exports = { MORE_LABEL_PATTERN, MORE_ICON_PATTERN };
