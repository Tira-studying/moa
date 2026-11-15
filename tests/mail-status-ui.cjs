/**
 * 가상 자료로 브라우저 화면 또는 계산 결과를 검증합니다.
 * 실제 거래·암호는 사용하지 않습니다. 브라우저 테스트는 Chromium 샌드박스를 켜고 실행합니다.
 */

const fs = require('fs'),
  path = require('path');
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
    let activeChecks = 0,
      maxActiveChecks = 0;
    const checkedIds = [];
    const errors = [];
    const page = await context.newPage();
    page.on('pageerror', (e) => errors.push(e.message));
    const items = ['received', 'needs_review', 'ready', 'saved'].map((stage, i) => ({
      id: String(i),
      filename: i === 0 ? '<img src=x onerror=alert(1)>.csv' : `fixture-${i}.csv`,
      stage,
      card: '신한',
      size: 100,
      state: 'waiting',
      created: '2026-10-30 00:00:00',
      saved_tables: stage === 'saved' ? 1 : 0,
      saved_count: stage === 'saved' ? 3 : 0,
      saved_at: '2026-10-30 01:00:00',
      auto_error: stage === 'needs_review' ? '비밀번호 확인 필요' : null,
    }));
    await context.route('**/*', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === '/')
        return route.fulfill({
          contentType: 'text/html',
          body: fs.readFileSync('templates/index.html', 'utf8'),
        });
      if (url.pathname.startsWith('/static/'))
        return route.fulfill({
          contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css',
          body: fs.readFileSync(path.join('.', url.pathname)),
        });
      if (url.pathname.endsWith('/check')) {
        const id = url.pathname.split('/')[3];
        checkedIds.push(id);
        activeChecks++;
        maxActiveChecks = Math.max(maxActiveChecks, activeChecks);
        await new Promise((resolve) => setTimeout(resolve, 200));
        activeChecks--;
        return route.fulfill(
          id === '1'
            ? { status: 400, json: { error: 'fixture check failed' } }
            : { json: { ok: true, count: 3, tables: 1 } },
        );
      }
      if (url.pathname === '/api/mail/refresh')
        return route.fulfill({ status: 202, json: { ticket: 1 } });
      if (url.pathname === '/api/data')
        return route.fulfill({ json: { transactions: [], imports: [] } });
      if (url.pathname === '/api/mail')
        return route.fulfill({
          json: {
            configured: true,
            folder: 'MOA',
            items,
            summary: { received: 1, needs_review: 1, ready: 1, saved: 1 },
            total: 4,
            status: null,
          },
        });
      return route.abort();
    });
    await page.goto('https://fixture.invalid');
    if (
      !(await page.locator('#dashboard').isVisible()) ||
      (await page.locator('#mail-inbox').isVisible())
    )
      throw Error('Only dashboard should be shown initially');
    await page.locator('.mobile-nav a[href="#sharing"]').click();
    await page.locator('#sharing .account-shortcuts a[href="#mail-inbox"]').click();
    await page.locator('.mail-item').last().waitFor();
    if ((await page.locator('.mail-item').count()) !== 3)
      throw Error('Saved items must be hidden by default');
    await page.locator('#mail-refresh').click();
    if ((await page.locator('.mail-item').count()) !== 3)
      throw Error('Default filter lost on refresh');
    await page.locator('#mail-filter').selectOption('');
    if ((await page.locator('.mail-item').count()) !== 4) throw Error('All items filter failed');
    if ((await page.locator('#mail-summary>div').count()) !== 4) throw Error('Missing counts');
    if (await page.locator('#mail-items img').count()) throw Error('Unsafe filename');
    await page.locator('#mail-filter').selectOption('saved');
    if ((await page.locator('.mail-item').count()) !== 1) throw Error('Filter failed');
    if (!(await page.locator('#mail-items').textContent()).includes('거래 3건'))
      throw Error('Saved count missing');
    await page.locator('[data-mail-card]').selectOption('현대');
    await page.locator('#mail-refresh').click();
    if ((await page.locator('.mail-item').count()) !== 1) throw Error('Filter lost on refresh');
    if ((await page.locator('[data-mail-card]').inputValue()) !== '현대')
      throw Error('Card selection lost');
    await page.locator('#mail-filter').selectOption('needs_review');
    if (!(await page.locator('#mail-items').textContent()).includes('비밀번호 확인 필요'))
      throw Error('Reason missing');
    await page.locator('#mail-filter').selectOption('pending');
    await page.locator('#mail-select-all').check();
    const firstCheck = page.waitForRequest((request) => request.url().endsWith('/check'));
    await page.locator('#mail-bulk').click();
    await firstCheck;
    await page.locator('#mail-bulk-stop').click();
    await page.waitForFunction(() =>
      document.querySelector('#mail-bulk-progress').textContent.includes('미처리 2개'),
    );
    if (checkedIds.length !== 1) throw Error('Cancel must stop before the next file');
    await page.locator('#mail-select-all').check();
    await page.locator('#mail-bulk').click();
    await page.waitForFunction(() =>
      document
        .querySelector('#mail-bulk-progress')
        .textContent.includes('성공 2개 · 확인 필요 1개'),
    );
    if (maxActiveChecks !== 1 || checkedIds.length !== 4)
      throw Error('Bulk checks must run sequentially and continue after failure');
    if ((await page.locator('#mail-bulk-results li').count()) !== 3)
      throw Error('Per-file results missing');
    if (!(await page.locator('#mail-bulk-results').textContent()).includes('fixture check failed'))
      throw Error('Failure detail missing');
    if ((await page.locator('[data-mail-select]:checked').count()) !== 1)
      throw Error('Only failed files should remain selected');
    for (let i = 0; i < 12; i++)
      items.push({ ...items[0], id: 'extra' + i, filename: 'extra.csv' });
    await page.locator('#mail-refresh').click();
    await page.waitForFunction(() => document.querySelectorAll('.mail-item').length === 10);
    if ((await page.locator('.mail-item').count()) !== 10)
      throw Error('Initial list must be limited to ten items');
    await page.locator('#mail-more').click();
    if ((await page.locator('.mail-item').count()) !== 15) throw Error('More items not displayed');
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth))
      throw Error('Mobile overflow');
    if (errors.length) throw Error(errors.join('\n'));
    await context.close();
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
