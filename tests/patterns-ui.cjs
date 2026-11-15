/**
 * 가상 자료로 브라우저 화면 또는 계산 결과를 검증합니다.
 * 실제 거래·암호는 사용하지 않습니다. 브라우저 테스트는 Chromium 샌드박스를 켜고 실행합니다.
 */

const fs = require('fs'),
  path = require('path'),
  assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      isMobile: true,
      hasTouch: true,
    });
    let rows = [
      {
        id: 1,
        used_on: '2026-10-03',
        merchant: '<img src=x onerror=alert(1)>',
        card: '삼성',
        amount: 100,
        category: '커피',
      },
      {
        id: 2,
        used_on: '2026-10-05',
        merchant: '<img src=x onerror=alert(1)>',
        card: '삼성',
        amount: 200,
        category: '커피',
      },
      {
        id: 3,
        used_on: '2026-10-05',
        merchant: 'fixture B',
        card: '신한',
        amount: 300,
        category: '식비',
      },
      {
        id: 4,
        used_on: '2026-10-06',
        merchant: 'fixture B',
        card: '신한',
        amount: -500,
        category: '식비',
      },
      { id: 5, used_on: '2026-09-01', merchant: 'fixture C', card: '삼성', amount: 1000 },
      { id: 6, used_on: '2026-08-01', merchant: 'fixture D', card: '삼성', amount: -100 },
      ...Array.from({ length: 25 }, (_, i) => ({
        id: i + 7,
        used_on: '2025-01-01',
        merchant: 'older fixture',
        card: '현대',
        amount: 10,
      })),
    ];
    const page = await context.newPage(),
      errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === '/')
        return route.fulfill({
          contentType: 'text/html; charset=utf-8',
          body: fs.readFileSync('templates/index.html', 'utf8'),
        });
      if (url.pathname.startsWith('/static/'))
        return route.fulfill({
          contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css',
          body: fs.readFileSync(path.join('.', url.pathname)),
        });
      if (url.pathname.endsWith('/category')) {
        const id = Number(url.pathname.split('/')[3]),
          category = route.request().postDataJSON().category;
        const merchant = rows.find((row) => row.id === id).merchant;
        rows = rows.map((row) =>
          row.merchant === merchant
            ? {
                ...row,
                category: category || '커피',
                category_source: category ? 'manual' : 'rule',
              }
            : row,
        );
        return route.fulfill({ json: { ok: true } });
      }
      if (url.pathname === '/api/data')
        return route.fulfill({ json: { transactions: rows, imports: [] } });
      if (url.pathname === '/api/mail')
        return route.fulfill({ json: { configured: false, items: [] } });
      return route.abort();
    });
    const nav = (id) => page.locator(`.mobile-nav a[href="#${id}"]`).click();
    const count = async (n) => {
      await page.waitForFunction(
        (n) => document.querySelector('#result-count').textContent === n + '건',
        n,
      );
    };
    await page.goto('https://fixture.invalid');
    await page.waitForFunction(() => document.querySelector('#tx-count').textContent.includes('4'));
    assert.equal(await page.locator('#pattern-average').textContent(), '200원');
    assert.equal(await page.locator('#pattern-weekend').textContent(), '17%');
    assert.equal(await page.locator('#pattern-trend [data-pattern-month]').count(), 6);
    assert.equal(await page.locator('#dashboard .transactions').count(), 0);
    await nav('activity');
    await count(31);
    assert.equal(await page.locator('#transactions tr').count(), 20);
    await page.locator('#more').click();
    assert.equal(await page.locator('#transactions tr').count(), 31);
    assert((await page.locator('#tx-summary').textContent()).includes('1,250원'));
    await page.locator('#tx-period').selectOption('month');
    await page.locator('#tx-month').fill('2026-10');
    await count(4);
    await page.locator('#tx-month').fill('');
    await count(0);
    assert((await page.locator('#tx-period-error').textContent()).includes('월'));
    await page.locator('#tx-period').selectOption('custom');
    await page.locator('#tx-start').fill('2026-10-05');
    await page.locator('#tx-end').fill('2026-10-05');
    await count(2);
    await page.locator('#tx-start').fill('2026-10-06');
    await count(0);
    assert((await page.locator('#tx-period-error').textContent()).includes('시작일'));
    await nav('dashboard');
    assert.equal(
      await page.locator('[data-pattern-tab="categories"]').getAttribute('aria-pressed'),
      'true',
    );
    await page.locator('[data-pattern-category="커피"]').click();
    await count(2);
    await page.locator('[data-category-id="2"]').selectOption('의료');
    await count(0);
    await nav('dashboard');
    assert.equal(await page.locator('[data-pattern-category="의료"]').count(), 1);
    await page.locator('[data-pattern-category="의료"]').click();
    await count(2);
    await page.locator('[data-category-id="2"]').selectOption('__auto__');
    await count(0);
    await nav('dashboard');
    assert.equal(await page.locator('[data-pattern-category="커피"]').count(), 1);
    await page.locator('[data-pattern-tab="cards"]').click();
    await page.locator('[data-pattern-card="삼성"]').click();
    await count(2);
    assert.equal(await page.locator('#tx-period').inputValue(), 'month');
    assert.equal(await page.locator('#filter-card').inputValue(), '삼성');
    await nav('dashboard');
    await page.locator('[data-pattern-tab="merchants"]').click();
    assert.equal(await page.locator('#pattern-distribution img').count(), 0);
    await page.locator('[data-pattern-merchant="fixture B"]').click();
    await count(2);
    assert((await page.locator('#tx-summary').textContent()).includes('-200원'));
    await nav('dashboard');
    await page.locator('[data-pattern-month="2026-09"]').click();
    await count(1);
    assert.equal(await page.locator('#search').inputValue(), '');
    assert.equal(await page.locator('#filter-card').inputValue(), '');
    await nav('dashboard');
    await page.locator('[data-pattern-tab="weekdays"]').click();
    assert.equal(await page.locator('#pattern-distribution .pattern-row').count(), 7);
    await page.locator('.dashboard-info summary').click();
    await page.locator('#demo').check();
    await nav('activity');
    await page.locator('#tx-period').selectOption('all');
    await count(31);
    assert.equal(await page.locator('#activity img').count(), 0);
    await nav('dashboard');
    if (process.env.MOA_UI_SCREENSHOT)
      await page.screenshot({
        path: path.join(process.env.MOA_UI_SCREENSHOT, 'patterns.png'),
        fullPage: true,
      });
    for (const width of [320, 390, 768, 1280]) {
      await page.setViewportSize({ width, height: 844 });
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
    }
    rows = [{ id: 1, used_on: '2026-10-01', merchant: 'fixture', card: '삼성', amount: -10 }];
    await page.reload();
    await page.waitForFunction(() =>
      document.querySelector('#refunds').textContent.includes('10원'),
    );
    assert.equal(await page.locator('#pattern-average').textContent(), '0원');
    rows = [];
    await page.reload();
    await page.waitForFunction(() =>
      document.querySelector('#pattern-distribution').textContent.includes('없습니다'),
    );
    assert.equal(await page.locator('#pattern-weekend').textContent(), '0%');
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
