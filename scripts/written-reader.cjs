/**
 * document.write로 생성된 결과를 스크립트 실행·외부 이동 없이 별도 페이지에서 읽습니다.
 * document.write는 명세서 스크립트가 HTML을 화면에 써 넣는 방식입니다.
 */

// Read document.write output without running its scripts or following any links.
const { MORE_LABEL_PATTERN, MORE_ICON_PATTERN } = require('./more-label.cjs');
const { inspectWritten } = require('./written-inspector.cjs');
const { extractTables } = require('./table-reader.cjs');
const { extractList } = require('./list-reader.cjs');
const { extractLotte } = require('./lotte-reader.cjs');
/** 기록한 HTML을 격리된 읽기 화면에 넣고 거래 후보를 추출합니다. */
async function readWritten(browser, html, card) {
  const context = await browser.newContext({
    javaScriptEnabled: false,
    serviceWorkers: 'block',
    acceptDownloads: false,
  });
  try {
    let delivered = false;
    await context.route('**/*', (r) => {
      if (!delivered && r.request().url() === 'https://written.invalid/') {
        delivered = true;
        return r.fulfill({
          contentType: 'text/html; charset=utf-8',
          body: html,
          headers: {
            'Content-Security-Policy':
              "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'",
          },
        });
      }
      return r.abort();
    });
    const page = await context.newPage();
    await page.goto('https://written.invalid/', { waitUntil: 'domcontentloaded', timeout: 3000 });
    const diagnostics = await page.evaluate(inspectWritten, html);
    // A static snapshot cannot load additional records. Require the live path.
    const pending = await page.evaluate(
      ({ pattern, iconPattern }) =>
        [
          ...document.querySelectorAll(
            'button,a,[role="button"],input[type="button"],input[type="image"],img[alt],img[src],[onclick]',
          ),
        ].some((e) => {
          const ownText = [];
          const walker = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
          let textNode;
          while ((textNode = walker.nextNode()) && ownText.length < 40) {
            if (textNode.parentElement.closest('a,button,[role="button"],[onclick]') === e)
              ownText.push(textNode.textContent);
          }
          const labels = [
            ...ownText,
            e.innerText,
            e.value,
            e.getAttribute('aria-label'),
            e.getAttribute('alt'),
            e.querySelector('img[alt]')?.alt,
          ]
            .filter(Boolean)
            .map((v) => v.normalize('NFKC').replace(/[\s↓▼▾+＋]/g, ''))
            .filter(Boolean);
          const label = labels[0] || '';
          const image = e.tagName === 'IMG' ? e : e.querySelector('img[src]');
          const iconMore =
            image &&
            !labels.some(Boolean) &&
            new RegExp(iconPattern, 'i').test((image.getAttribute('src') || '').split('?')[0]);
          const pages = (e.innerText || '')
            .normalize('NFKC')
            .match(/[(]\D*(\d+)\D*[/]\D*(\d+)\D*[)]/);
          if (pages && Number(pages[2]) > 0 && Number(pages[1]) >= Number(pages[2])) return false;
          return (
            e.getClientRects().length &&
            getComputedStyle(e).visibility !== 'hidden' &&
            !e.closest('[hidden]') &&
            !e.disabled &&
            (iconMore ||
              labels.some((v) => new RegExp(pattern).test(v)) ||
              (e.getAttribute('aria-expanded') === 'false' && /^(일시불|할부)/.test(label)))
          );
        }),
      { pattern: MORE_LABEL_PATTERN, iconPattern: MORE_ICON_PATTERN },
    );
    const listed = await page.evaluate(card === '롯데' ? extractLotte : extractList);
    if (listed.oversized) return { error: 'size' };
    const extracted = listed.tables.length ? listed : await page.evaluate(extractTables);
    if (extracted.oversized) return { error: 'size' };
    const tables = extracted.tables;
    if (
      tables.length &&
      (pending || (listed.expectedCount != null && listed.readCount !== listed.expectedCount))
    )
      return { error: 'incomplete' };
    return { tables, diagnostics };
  } finally {
    await context.close();
  }
}
module.exports = { readWritten };
