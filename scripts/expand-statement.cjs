/**
 * 더보기 처리: 페이지의 버튼·건수 표시를 확인하면서 추가 거래를 펼칩니다.
 * 확인한 전체 건수와 실제 후보 수가 맞지 않으면 일부만 저장하도록 허용하지 않습니다.
 */

const { MORE_LABEL_PATTERN, MORE_ICON_PATTERN } = require('./more-label.cjs');
// Expand statement controls; official HTTPS pagination is opt-in.
/** 더보기 클릭과 건수 확인을 반복하고 완료 여부·진단을 반환합니다. */
async function expandStatement(page, { allowOnline = false } = {}) {
  for (const frame of page.frames()) {
    const candidate = await frame
      .evaluate(
        ({ allowOnline, morePattern, iconPattern }) => {
          const moreLabel = new RegExp(morePattern);
          const pending = window.__moaExpansionPending;
          const visible = (element) =>
            element &&
            element.isConnected &&
            element.getClientRects().length &&
            getComputedStyle(element).visibility !== 'hidden' &&
            !element.closest('[hidden]');
          // CSS-only expansion can leave innerText unchanged (e.g. max-height).
          if (
            pending &&
            visible(pending.element) &&
            document.body.innerText === pending.text &&
            document.body.innerHTML === pending.html &&
            document.body.scrollHeight === pending.height
          ) {
            if (Date.now() - pending.time < (allowOnline ? 15000 : 5000)) return { waiting: true };
            return { stalled: true, reason: 1 };
          }
          window.__moaExpansionPending = null;
          for (const details of document.querySelectorAll('details:not([open])'))
            details.open = true;
          const controls = [
            ...document.querySelectorAll(
              'button,a,[role="button"],input[type="button"],input[type="image"],img[alt],img[src],[onclick],[tabindex]',
            ),
          ];
          for (const element of controls) {
            const image = element.querySelector('img[alt],img[src]');
            const ownedImage =
              image && image.closest('a,button,[role="button"],[onclick]') === element;
            const ownText = [];
            const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
            let textNode;
            while ((textNode = walker.nextNode()) && ownText.length < 40) {
              if (textNode.parentElement.closest('a,button,[role="button"],[onclick]') === element)
                ownText.push(textNode.textContent);
            }
            const labels = [
              ...ownText,
              element.innerText,
              element.value,
              element.getAttribute('aria-label'),
              element.getAttribute('title'),
              element.getAttribute('alt'),
              ownedImage ? image.alt : '',
            ]
              .filter(Boolean)
              .map((v) => v.normalize('NFKC').replace(/[\s↓▼▾+＋]/g, ''))
              .filter(Boolean);
            const label = labels[0] || '';
            const icon = element.tagName === 'IMG' ? element : ownedImage ? image : null;
            const iconMore =
              icon &&
              !labels.some(Boolean) &&
              new RegExp(iconPattern, 'i').test((icon.getAttribute('src') || '').split('?')[0]);
            const more = labels.some((value) => moreLabel.test(value)) || iconMore;
            const collapsed =
              element.getAttribute('aria-expanded') === 'false' && /^(일시불|할부)/.test(label);
            if (!more && !collapsed) continue;
            const pages = (element.innerText || '')
              .normalize('NFKC')
              .match(/[(]\D*(\d+)\D*[/]\D*(\d+)\D*[)]/);
            if (more && pages && Number(pages[2]) > 0 && Number(pages[1]) >= Number(pages[2]))
              continue;
            if (
              !element.getClientRects().length ||
              getComputedStyle(element).visibility === 'hidden' ||
              element.closest('[hidden]') ||
              element.disabled ||
              element.getAttribute('aria-disabled') === 'true'
            )
              continue;
            const href = element.closest('a[href]')?.getAttribute('href');
            if (href && !href.startsWith('#') && !href.toLowerCase().startsWith('javascript:')) {
              let official = false;
              try {
                const u = new URL(href, location.href);
                official =
                  allowOnline &&
                  u.protocol === 'https:' &&
                  (!u.port || u.port === '443') &&
                  (u.hostname === 'samsungcard.com' || u.hostname.endsWith('.samsungcard.com'));
              } catch {}
              if (!official) continue;
            }
            const length = document.body.innerText.length;
            if (element.__moaLength === length && element.__moaAttempts >= 2)
              return { stalled: true };
            element.__moaAttempts =
              element.__moaLength === length ? (element.__moaAttempts || 0) + 1 : 1;
            element.__moaLength = length;
            const id = String((window.__moaMoreSequence = (window.__moaMoreSequence || 0) + 1));
            element.setAttribute('data-moa-more', id);
            window.__moaExpansionPending = {
              element,
              text: document.body.innerText,
              html: document.body.innerHTML,
              height: document.body.scrollHeight,
              time: Date.now(),
            };
            return { id };
          }
          return null;
        },
        { allowOnline, morePattern: MORE_LABEL_PATTERN, iconPattern: MORE_ICON_PATTERN },
      )
      .catch(() => null);
    if (candidate?.waiting) return { waiting: true };
    if (candidate?.stalled) return { stalled: true, reason: candidate.reason || 1 };
    if (candidate?.id) {
      try {
        await frame
          .locator(`[data-moa-more="${candidate.id}"]`)
          .click({ timeout: 1000, noWaitAfter: true });
      } catch {
        return { stalled: true, reason: 2 };
      }
      await new Promise((resolve) => setTimeout(resolve, 150));
      return { clicked: true };
    }
  }
  return { clicked: false };
}
module.exports = { expandStatement };
