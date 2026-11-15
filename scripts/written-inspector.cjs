/**
 * document.write 결과의 구조만 조사합니다.
 * 스크립트·폼·이동 대상의 건수만 반환하고 원문·주소·토큰은 진단에 넣지 않습니다.
 */

// Return structure counts only. Never return text, URLs, tokens, or attributes.
/** 외부 이동 가능성 등 결과 HTML의 구조 건수만 셉니다. */
function inspectWritten(html) {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  let externalDestinations = 0,
    samsungDestinations = 0,
    writtenNavigationScripts = 0;
  const consider = (value) => {
    try {
      const url = new URL(value, doc.baseURI || 'https://statement.invalid/');
      if (
        !['http:', 'https:'].includes(url.protocol) ||
        ['statement.invalid', 'written.invalid'].includes(url.hostname)
      )
        return;
      externalDestinations++;
      if (url.hostname === 'samsungcard.com' || url.hostname.endsWith('.samsungcard.com'))
        samsungDestinations++;
    } catch (_) {}
  };
  for (const element of doc.querySelectorAll(
    'a[href],form[action],iframe[src],frame[src],script[src]',
  ))
    consider(
      element.getAttribute(
        element.hasAttribute('href') ? 'href' : element.hasAttribute('action') ? 'action' : 'src',
      ),
    );
  for (const meta of doc.querySelectorAll('meta[http-equiv]')) {
    if (meta.getAttribute('http-equiv').toLowerCase() === 'refresh') {
      const match = (meta.getAttribute('content') || '').match(/url\s*=\s*["']?([^"']+)/i);
      if (match) consider(match[1].trim());
    }
  }
  for (const script of doc.querySelectorAll('script:not([src])')) {
    const text = script.textContent;
    if (/(?:location(?:\.href)?|window\.open|\.submit\s*\()/.test(text)) writtenNavigationScripts++;
    // Only literal navigation targets; never execute or return the script.
    const patterns = [
      /(?:location(?:\.href)?\s*=|location\.(?:assign|replace)\s*\(|window\.open\s*\()\s*["']([^"']+)["']/g,
    ];
    for (const pattern of patterns) for (const match of text.matchAll(pattern)) consider(match[1]);
  }
  return {
    writtenNavigationScripts,
    writtenScripts: doc.scripts.length,
    writtenForms: doc.forms.length,
    writtenLinks: doc.querySelectorAll('a[href]').length,
    writtenFrames: doc.querySelectorAll('iframe,frame').length,
    externalDestinations,
    samsungDestinations,
  };
}
module.exports = { inspectWritten };
