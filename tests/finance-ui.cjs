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
    let plans = [],
      decision = 'candidate';
    const rows = [
      {
        id: 1,
        import_id: 1,
        used_on: '2026-10-05',
        merchant: '가상 구독',
        card: '삼성',
        amount: 1200,
        excluded: 0,
        filename: 'email.html',
        imported_at: '2026-10-30 00:00:00',
      },
      {
        id: 2,
        import_id: 2,
        used_on: '2026-10-05',
        merchant: '가상 구독',
        card: '삼성',
        amount: 1200,
        excluded: 0,
        filename: 'manual.csv',
        imported_at: '2026-10-30 00:00:00',
      },
      {
        id: 3,
        import_id: 1,
        used_on: '2026-09-05',
        merchant: '가상 구독',
        card: '삼성',
        amount: 1000,
        excluded: 0,
      },
      {
        id: 4,
        import_id: 1,
        used_on: '2026-08-05',
        merchant: '가상 구독',
        card: '삼성',
        amount: 1000,
        excluded: 0,
      },
    ];
    function report() {
      const list = plans.map((plan) => ({
        ...plan,
        remaining_count: plan.months - plan.paid_count,
        remaining_principal:
          plan.principal - plan.paid_count * Math.floor(plan.principal / plan.months),
        regular_amount: 33,
        final_amount: 34,
        end_month: '2026-12',
        next_month: plan.paid_count ? '2026-11' : '2026-10',
      }));
      return {
        installments: {
          plans: list,
          remaining_principal: list.reduce((sum, p) => sum + p.remaining_principal, 0),
          this_month: list.length && !list[0].paid_count ? 33 : 0,
          past_unconfirmed: 0,
          schedule: Array.from({ length: 12 }, (_, i) => ({
            month: `2026-${String(i + 1).padStart(2, '0')}`,
            amount: 0,
          })),
        },
        duplicates: !rows[1].excluded
          ? [
              {
                card: '삼성',
                used_on: rows[0].used_on,
                merchant: rows[0].merchant,
                amount: 1200,
                items: rows.slice(0, 2),
              },
            ]
          : [],
        excluded: rows.filter((row) => row.excluded),
        recurring: rows[1].excluded
          ? [
              {
                card: '삼성',
                merchant: '가상 구독',
                transaction_id: 1,
                amount: 1200,
                previous_amount: 1000,
                change: 200,
                occurrences: 3,
                last_used_on: '2026-10-05',
                next_expected: '2026-11-05',
                status: decision,
                history: [rows[3], rows[2], rows[0]],
              },
            ]
          : [],
      };
    }
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url()),
        method = route.request().method();
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
        return route.fulfill({ json: { transactions: rows, imports: [] } });
      if (url.pathname === '/api/mail')
        return route.fulfill({ json: { configured: false, items: [] } });
      if (url.pathname === '/api/finance') return route.fulfill({ json: report() });
      if (url.pathname.startsWith('/api/installments')) {
        if (method === 'DELETE') plans = [];
        else {
          const data = route.request().postDataJSON();
          plans = [
            {
              ...data,
              id: 1,
              principal: Number(data.principal),
              months: Number(data.months),
              paid_count: Number(data.paid_count),
            },
          ];
        }
        return route.fulfill({ json: { id: 1, ok: true } });
      }
      if (url.pathname.endsWith('/exclusion')) {
        const id = Number(url.pathname.split('/')[3]);
        rows.find((row) => row.id === id).excluded = route.request().postDataJSON().excluded
          ? 1
          : 0;
        return route.fulfill({ json: { ok: true } });
      }
      if (url.pathname.endsWith('/recurring')) {
        decision = route.request().postDataJSON().status || 'candidate';
        return route.fulfill({ json: { ok: true } });
      }
      return route.abort();
    });
    await page.goto('https://fixture.invalid');
    await page.waitForFunction(() => document.querySelector('#tx-count').textContent.includes('2'));
    await page.locator('.mobile-nav a[href="#planning"]').click();
    await page.locator('#finance-month').fill('2026-10');
    await page.waitForFunction(() =>
      document.querySelector('#finance-state').textContent.includes('저장된 거래'),
    );
    await page.locator('#installment-editor summary').click();
    await page.locator('#plan-name').fill('<img src=x onerror=alert(1)>');
    await page.locator('#plan-card').selectOption('우리');
    await page.locator('#plan-principal').fill('100');
    await page.locator('#plan-months').fill('3');
    await page.locator('#plan-first-month').fill('2026-10');
    await page.locator('#plan-save').click();
    await page.locator('[data-plan-paid="1"]').waitFor();
    assert((await page.locator('#installment-list').textContent()).includes('100원'));
    assert.equal(await page.locator('#installment-list img').count(), 0);
    await page.locator('[data-plan-paid="1"]').click();
    await page.waitForFunction(() =>
      document.querySelector('#installment-list').textContent.includes('67원'),
    );
    await page.locator('[data-plan-edit="1"]').click();
    assert.equal(await page.locator('#plan-paid').inputValue(), '1');
    await page.locator('[data-finance-tab="duplicates"]').click();
    assert.equal(await page.locator('[data-exclude]').count(), 2);
    await page.locator('[data-exclude="2"]').click();
    await page.waitForFunction(() =>
      document.querySelector('#duplicate-list').textContent.includes('없습니다'),
    );
    assert.equal(await page.locator('#total').textContent(), '1,200원');
    await page.locator('[data-finance-tab="recurring"]').click();
    assert((await page.locator('#recurring-list').textContent()).includes('200원 증가'));
    await page.locator('[data-recurring-status="confirmed"]').click();
    await page.waitForFunction(() =>
      document.querySelector('#recurring-list').textContent.includes('고정 지출로 확인'),
    );
    await page.locator('[data-recurring-status="ignored"]').click();
    await page.waitForFunction(() =>
      document.querySelector('#recurring-list').textContent.includes('후보가 없습니다'),
    );
    await page.locator('[data-finance-panel="recurring"]>details>summary').click();
    await page.locator('[data-recurring-status="reset"]').click();
    await page.locator('.mobile-nav a[href="#activity"]').click();
    await page.locator('#activity-filter-toggle').click();
    await page.locator('#tx-visibility').selectOption('excluded');
    await page.locator('[data-ledger-restore="2"]').click();
    await page.waitForFunction(() => document.querySelector('#total').textContent === '2,400원');
    await page.locator('.mobile-nav a[href="#planning"]').click();
    await page.locator('[data-finance-tab="installments"]').click();
    page.once('dialog', (dialog) => dialog.accept());
    await page.locator('[data-plan-delete="1"]').click();
    await page.waitForFunction(() =>
      document.querySelector('#installment-list').textContent.includes('등록한 할부가 없습니다'),
    );
    assert.equal(rows.length, 4);
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
