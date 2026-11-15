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
      }),
      page = await context.newPage(),
      errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const providers = new Map([
        ['네이버페이', '네이버페이'],
        ['카카오페이', '카카오페이'],
        ['토스페이', '토스페이'],
        ['페이코', '페이코'],
      ]),
      shared = new Map();
    const rows = [
      '네이버페이',
      '네이버페이',
      '스타벅스',
      '스타벅스',
      '카카오페이',
      '토스페이',
      '페이코',
    ].map((merchant, i) => ({
      id: i + 1,
      used_on: `2026-10-${String(i + 1).padStart(2, '0')}`,
      card: '삼성',
      merchant,
      amount: (i + 1) * 1000,
      transaction_category: null,
      category_reviewed: true,
      actual_merchant: '',
      memo: '',
    }));
    rows.push({
      id: 8,
      used_on: '2026-10-08',
      card: '현대',
      merchant: '가상 생활 서비스 온라인 결제 가맹점 이름이 긴 경우',
      amount: -12500,
      transaction_category: null,
      category_reviewed: true,
      actual_merchant: '',
      memo: '가상 환불 내역',
    });
    let importedActual = false;
    function data() {
      return rows.map((row) => {
        const name = row.actual_merchant || row.merchant,
          rule = name.includes('약국')
            ? '의료'
            : name.includes('카페') || name === '스타벅스'
              ? '커피'
              : '미분류';
        const category = row.transaction_category || shared.get(name) || rule;
        return {
          ...row,
          category,
          payment_service: providers.get(row.merchant) || null,
          category_source: row.transaction_category
            ? 'transaction'
            : shared.has(name)
              ? 'manual'
              : rule === '미분류'
                ? 'unclassified'
                : 'rule',
          category_scope:
            providers.has(row.merchant) || row.transaction_category ? 'transaction' : 'merchant',
        };
      });
    }
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
      if (url.pathname === '/api/data')
        return route.fulfill({ json: { transactions: data(), imports: [] } });
      if (url.pathname === '/api/mail')
        return route.fulfill({ json: { configured: false, items: [] } });
      if (url.pathname.endsWith('/category')) {
        const row = rows.find((row) => row.id === Number(url.pathname.split('/')[3])),
          payload = route.request().postDataJSON();
        if (providers.has(row.merchant)) assert.equal(payload.scope, 'transaction');
        if ('actual_merchant' in payload) row.actual_merchant = payload.actual_merchant;
        if ('memo' in payload) row.memo = payload.memo;
        if (payload.update_category !== false) {
          if (payload.scope === 'transaction') row.transaction_category = payload.category;
          else {
            row.transaction_category = null;
            const key = row.actual_merchant || row.merchant;
            if (payload.category) shared.set(key, payload.category);
            else shared.delete(key);
          }
        }
        return route.fulfill({ json: { ok: true, scope: payload.scope } });
      }
      if (url.pathname === '/api/preview')
        return route.fulfill({
          json: {
            token: 'fixture',
            columns: ['이용일', '가맹점', '금액', '실제 가맹점'],
            count: 1,
            rows: [['2026-10-10', '네이버페이', '1000', '가상카페']],
          },
        });
      if (url.pathname === '/api/import') {
        importedActual = route.request().postDataJSON().actual_merchant === '3';
        return route.fulfill({ json: { count: 1 } });
      }
      return route.abort();
    });
    await page.goto('https://fixture.invalid/#activity');
    await page.locator('[data-category-id="1"]').waitFor();
    await page.locator('#tx-period').selectOption('month');
    await page.locator('#tx-month').fill('2026-10');
    assert.equal(await page.locator('#activity-filters').isHidden(), true);
    assert.equal(await page.locator('#confirm-category-reviews').isHidden(), true);
    // 문서 전체뿐 아니라 표 내부와 금액·카드·편집 버튼도 가로로 잘리지 않는지 확인합니다.
    for (const width of [320, 375, 390, 430]) {
      await page.setViewportSize({ width, height: 844 });
      await page.evaluate(() => window.scrollTo(0, 0));
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
      assert.equal(
        await page
          .locator('#activity .table-wrap')
          .evaluate((el) => el.scrollWidth > el.clientWidth),
        false,
      );
      for (const selector of [
        '.transaction-amount',
        '.transaction-card',
        '.category-edit select',
        '.detail-button',
      ]) {
        assert.equal(
          await page
            .locator('#transactions tr')
            .first()
            .locator(selector)
            .evaluate((el) => {
              const box = el.getBoundingClientRect();
              return box.left >= 0 && box.right <= innerWidth;
            }),
          true,
        );
      }
      assert.equal(
        await page
          .locator('#transactions tr')
          .first()
          .evaluate((el) => el.getBoundingClientRect().top < 480),
        true,
      );
      if (process.env.MOA_UI_SCREENSHOT && width === 375)
        await page.screenshot({
          path: path.join(process.env.MOA_UI_SCREENSHOT, 'activity-mobile.png'),
          fullPage: true,
        });
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#activity-filter-toggle').click();
    await page.locator('#filter-card').selectOption('현대');
    assert.equal(await page.locator('#transactions tr').count(), 1);
    assert.equal(await page.locator('#activity-filter-count').textContent(), '1');
    await page.locator('#activity-filter-toggle').click();
    assert.equal(await page.locator('#activity-filters').isHidden(), true);
    await page.locator('#activity-filter-toggle').click();
    await page.locator('#activity-filters-reset').click();
    assert.equal(await page.locator('#activity-filter-count').isHidden(), true);
    await page.locator('#activity-filter-toggle').click();
    await page.locator('#tx-period').selectOption('all');
    await page.locator('[data-category-id="1"]').selectOption('식비');
    await page.waitForFunction(
      () =>
        document.querySelector('[data-category-id="1"]').value === '식비' &&
        !document.querySelector('[data-category-id="1"]').disabled,
    );
    assert.equal(await page.locator('[data-category-id="2"]').inputValue(), '미분류');
    await page.locator('[data-category-id="3"]').selectOption('쇼핑');
    await page.waitForFunction(
      () => document.querySelector('[data-category-id="4"]').value === '쇼핑',
    );
    await page.locator('[data-transaction-details="2"]').click();
    await page.locator('#transaction-editor').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#transaction-scope').inputValue(), 'transaction');
    assert.equal(
      await page
        .locator('#transaction-scope option[value="merchant"]')
        .evaluate((el) => el.disabled),
      true,
    );
    await page.locator('#transaction-actual').fill('가상 약국');
    await page.locator('#transaction-memo').fill('처방약 <img src=x onerror=alert(1)>');
    await page.locator('#transaction-edit-save').click();
    await page.waitForFunction(
      () => document.querySelector('[data-category-id="2"]').value === '의료',
    );
    assert.equal(await page.locator('#activity img').count(), 0);
    assert.equal(rows[1].merchant, '네이버페이');
    await page.locator('[data-transaction-details="2"]').click();
    await page.locator('#transaction-category').selectOption('커피');
    await page.locator('#transaction-edit-save').click();
    await page.waitForFunction(
      () => document.querySelector('[data-category-id="2"]').value === '커피',
    );
    assert.equal(await page.locator('[data-category-id="1"]').inputValue(), '식비');
    await page.locator('[data-category-id="2"]').selectOption('__auto__');
    await page.waitForFunction(
      () => document.querySelector('[data-category-id="2"]').value === '의료',
    );
    await page.locator('[data-transaction-details="3"]').click();
    await page.locator('#transaction-scope').selectOption('transaction');
    await page.locator('#transaction-category').selectOption('의료');
    await page.locator('#transaction-edit-save').click();
    await page.waitForFunction(
      () => document.querySelector('[data-category-id="3"]').value === '의료',
    );
    assert.equal(await page.locator('[data-category-id="4"]').inputValue(), '쇼핑');
    await page.locator('#search').fill('처방약');
    assert.equal(await page.locator('#transactions tr').count(), 1);
    await page.locator('.mobile-nav a[href="#upload"]').click();
    await page.locator('#upload-card').selectOption('삼성');
    await page.locator('#file').setInputFiles({
      name: 'fixture.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from('synthetic'),
    });
    await page.locator('#preview-button').click();
    await page.locator('#mapping').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#map-actual-merchant').inputValue(), '3');
    await page.locator('#import-button').click();
    await page.waitForFunction(() =>
      document.querySelector('#status').textContent.includes('1건을 저장'),
    );
    assert.equal(importedActual, true);
    await page.locator('.mobile-nav a[href="#activity"]').click();
    await page.locator('#search').fill('');
    await page.locator('[data-transaction-details="1"]').click();
    for (const width of [320, 390, 768, 1280]) {
      await page.setViewportSize({ width, height: 844 });
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
    }
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
