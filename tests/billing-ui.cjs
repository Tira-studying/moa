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
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } }),
      errors = [],
      stored = new Map(),
      settings = new Map();
    page.on('pageerror', (error) => errors.push(error.message));
    const cards = ['신한', '삼성', '현대', 'KB국민', '우리', '롯데'];
    const report = (month) => {
      const rows = cards.map((card) => ({
        card,
        enabled: true,
        due_day: null,
        statement_status: 'missing',
        amount: null,
        paid: false,
        due_on: null,
        due_override: null,
        ...settings.get(card),
        ...stored.get(month + card),
      }));
      const active = rows.filter((r) => r.enabled);
      return {
        month,
        cards: rows,
        total: active.reduce((n, r) => n + (Number(r.amount) || 0), 0),
        unpaid: active.reduce((n, r) => n + (r.paid ? 0 : Number(r.amount) || 0), 0),
        missing_count: active.filter((r) => r.statement_status === 'missing').length,
        unknown_count: active.filter((r) => r.amount === null && r.statement_status !== 'no_bill')
          .length,
      };
    };
    await page.route('**/*', (route) => {
      const req = route.request(),
        url = new URL(req.url());
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
        return route.fulfill({ json: { transactions: [], imports: [] } });
      if (url.pathname === '/api/mail')
        return route.fulfill({ json: { configured: false, items: [] } });
      if (url.pathname === '/api/billing')
        return route.fulfill({ json: report(url.searchParams.get('month')) });
      if (url.pathname === '/api/finance')
        return route.fulfill({
          json: {
            installments: {
              plans: [],
              remaining_principal: 0,
              this_month: 0,
              past_unconfirmed: 0,
              schedule: [],
            },
            duplicates: [],
            excluded: [],
            recurring: [],
          },
        });
      if (req.method() === 'PUT' && url.pathname.startsWith('/api/billing/')) {
        const parts = decodeURIComponent(url.pathname).split('/'),
          payload = req.postDataJSON();
        assert.equal(req.headers()['x-requested-with'], 'CardDashboard');
        if (parts[3] === 'settings') settings.set(parts[4], payload);
        else {
          if (payload.paid && payload.amount === null)
            return route.fulfill({ status: 400, json: { error: '청구액을 입력해 주세요.' } });
          stored.set(parts[3] + parts[4], {
            ...payload,
            amount: payload.amount === null ? null : Number(payload.amount),
            due_override: payload.due_on,
          });
        }
        return route.fulfill({ json: { ok: true } });
      }
      return route.fulfill({ status: 404, json: { error: 'missing' } });
    });
    await page.goto('http://moa.test');
    assert.equal(await page.locator('#open-billing').count(), 0);
    await page.locator('#dashboard-bill-open').click();
    await page.waitForSelector('.bill-form');
    await page.locator('#finance-month').fill('2026-10');
    await page.locator('#finance-month').dispatchEvent('change');
    const form = page.locator('.bill-form[data-card="삼성"]');
    await form.locator('[name=statement_status]').selectOption('received');
    await form.locator('[name=amount]').fill('120000');
    await form.locator('[name=due_on]').fill('2026-10-26');
    await form.locator('button').click();
    await page.waitForFunction(() =>
      document.querySelector('#billing-stats').textContent.includes('120,000원'),
    );
    await form.locator('[name=paid]').check();
    await form.locator('button').click();
    await page.waitForFunction(() =>
      [...document.querySelectorAll('.bill-form')]
        .find((f) => f.dataset.card === '삼성')
        ?.textContent.includes('납부 완료'),
    );
    assert.equal(stored.get('2026-10삼성').paid, true);
    await page.locator('#finance-month').fill('2026-11');
    await page.locator('#finance-month').dispatchEvent('change');
    await page.waitForFunction(
      // 다시 그리는 사이 양식이 잠깐 없을 수 있어 null이면 계속 기다립니다.
      () => document.querySelector('.bill-form')?.dataset.month === '2026-11',
    );
    assert.equal(await form.locator('[name=amount]').inputValue(), '');
    await form.locator('[name=statement_status]').selectOption('received');
    await form.locator('[name=paid]').check();
    await form.locator('button').click();
    await page.waitForFunction(() =>
      document
        .querySelector('.bill-form[data-card="삼성"] .bill-error')
        ?.textContent.includes('청구액'),
    );
    assert.equal(await form.locator('button').isEnabled(), true);
    await page.locator('#billing-settings').evaluate((el) => (el.parentElement.open = true));
    const setting = page.locator('.bill-setting[data-card="롯데"]');
    await setting.locator('[name=enabled]').uncheck();
    await setting.locator('[name=due_day]').fill('31');
    await setting.locator('button').click();
    await page.waitForFunction(() => !document.querySelector('.bill-form[data-card="롯데"]'));
    for (const width of [320, 390, 768, 1280]) {
      await page.setViewportSize({ width, height: 844 });
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
