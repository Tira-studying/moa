/** 가상 두 계정·실제 서버로 새 대시보드, 같은 기간 비교, 신규 분야 확인과 작은 화면을 검증합니다. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const base = process.env.MOA_TEST_URL;
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage(),
      errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.addInitScript(() => {
      const OriginalDate = Date;
      const now = OriginalDate.parse('2026-11-03T01:00:00+09:00');
      window.Date = class extends OriginalDate {
        constructor(...args) {
          super(...(args.length ? args : [now]));
        }
        static now() {
          return now;
        }
      };
    });
    await page.goto(base + '/login');
    await page.locator('#login-password').fill('fixture-owner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');
    await page.waitForFunction(() =>
      document.querySelector('#total').textContent.includes('3,000'),
    );
    assert.match(await page.locator('#comparison').textContent(), /전월 같은 기간.*2,500원 증가/);
    assert.equal(await page.locator('.dashboard-metrics > .stat').count(), 6);
    assert.equal(await page.locator('#pattern-trend [data-pattern-month]').count(), 6);
    assert.match(await page.locator('[data-pattern-month="2026-07"]').textContent(), /미등록/);
    assert.match(await page.locator('[data-pattern-month="2026-11"]').textContent(), /진행 중/);
    assert.equal(
      await page
        .locator('#pattern-trend')
        .evaluate((el) => el.getBoundingClientRect().bottom < 900),
      true,
    );
    assert.equal(await page.locator('#comparison-chart svg').count(), 1);
    assert.equal(
      await page.locator('#category-review-summary').textContent(),
      '새 사용내역 2건 · 소비 분야를 확인해 주세요',
    );
    if (process.env.MOA_SCREENSHOT_DIR)
      await page.screenshot({ path: process.env.MOA_SCREENSHOT_DIR + '/dashboard-desktop.png' });
    await page.locator('#open-category-review').click();
    await page.waitForURL(base + '/#activity');
    assert.equal(await page.locator('#tx-review-only').isChecked(), true);
    assert.equal(await page.locator('#transactions tr').count(), 2);
    await page.locator('#confirm-category-reviews').click();
    await page.waitForFunction(() => document.querySelector('#result-count').textContent === '0건');
    await page.goto(base + '/#dashboard');
    assert.equal(await page.locator('#open-category-review').isHidden(), true);
    // 실제 가져오기 API로 새 원장을 등록합니다. 금융기관 요청과 운영 자료는 사용하지 않습니다.
    const headers = { 'X-Requested-With': 'CardDashboard' };
    const preview = await context.request.post(base + '/api/preview', {
      headers,
      multipart: {
        file: {
          name: 'new-fixture.csv',
          mimeType: 'text/csv',
          buffer: Buffer.from('이용일,가맹점,금액\n2026-11-03,새가상상점,500'),
        },
        header: '1',
      },
    });
    assert.equal(preview.status(), 200);
    const revision = (await context.request.get(base + '/api/data')).headers()['x-moa-revision'];
    const result = await context.request.post(base + '/api/import', {
      headers: { ...headers, 'If-Match': revision },
      data: { token: (await preview.json()).token, card: '삼성', date: 0, merchant: 1, amount: 2 },
    });
    assert.equal(result.status(), 200);
    await page.evaluate(() => pollCategoryReviews());
    assert.match(await page.locator('#category-review-summary').textContent(), /새 사용내역 1건/);
    await page.setViewportSize({ width: 375, height: 900 });
    assert.equal(await page.locator('.mobile-nav a').count(), 5);
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
      false,
    );
    assert.equal(
      await page.locator('#total').evaluate((el) => el.getBoundingClientRect().top < 220),
      true,
    );
    if (process.env.MOA_SCREENSHOT_DIR)
      await page.screenshot({
        path: process.env.MOA_SCREENSHOT_DIR + '/dashboard-mobile.png',
        fullPage: true,
      });
    await page.locator('#open-category-review').click();
    await page.waitForURL(base + '/#activity');
    assert.equal(await page.locator('#transactions tr').count(), 1);
    const select = page.locator('[data-category-id]').first();
    await select.selectOption({ label: '육아' });
    await page.waitForFunction(() => document.querySelector('#result-count').textContent === '0건');
    await page.goto(base + '/#dashboard');
    await page.reload();
    await page.waitForFunction(() =>
      document.querySelector('#total').textContent.includes('3,500'),
    );
    assert.equal(await page.locator('#open-category-review').isHidden(), true);
    await page.locator('.mobile-nav a[href="#sharing"]').click();
    await page.locator('.account-shortcuts a[href="#history"]').click();
    assert.equal(await page.locator('#history').isVisible(), true);
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
