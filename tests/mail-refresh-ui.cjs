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
      page = await context.newPage();
    let ticket = 0,
      polls = 0,
      mode = 'normal';
    const errors = [];
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
      if (url.pathname === '/api/data')
        return route.fulfill({ json: { transactions: [], imports: [] } });
      if (url.pathname === '/api/mail/refresh') {
        assert.equal(route.request().method(), 'POST');
        assert.equal(route.request().headers()['x-requested-with'], 'CardDashboard');
        ticket++;
        polls = 0;
        return route.fulfill({ status: 202, json: { ticket } });
      }
      if (url.pathname === '/api/mail') {
        const busy = ticket > 0 && ++polls === 1,
          now = Math.floor(Date.now() / 1000),
          offline = mode === 'offline';
        const items =
          ticket > 0 && !busy
            ? [
                {
                  id: 'new',
                  filename: 'new-statement.csv',
                  card: '우리',
                  size: 100,
                  state: 'waiting',
                  stage: 'received',
                  created: '2026-10-30 00:00:00',
                },
              ]
            : [];
        return route.fulfill({
          json: {
            configured: true,
            folder: 'MOA',
            items,
            total: items.length,
            summary: { received: items.length },
            refresh: ticket
              ? {
                  requested: ticket,
                  completed: offline ? ticket - 1 : busy ? ticket - 1 : ticket,
                  requested_at: offline ? now - 30 : now,
                  heartbeat: now,
                  worker_online: !offline,
                  unresponsive: offline,
                  running: busy && !offline,
                  remaining: busy ? 1 : 0,
                  error: mode === 'error' ? '가상 인증 오류' : null,
                }
              : null,
          },
        });
      }
      return route.abort();
    });
    await page.goto('https://fixture.invalid/#mail-inbox');
    await page.locator('#mail-refresh').waitFor();
    await page.locator('#mail-refresh').click();
    assert.equal(ticket, 1);
    assert.equal(await page.locator('#mail-refresh').isDisabled(), true);
    await page.waitForFunction(() => !document.querySelector('#mail-refresh').disabled);
    assert.equal(await page.locator('[data-mail-open="new"]').count(), 1);
    mode = 'offline';
    await page.locator('#mail-refresh').click();
    await page.waitForFunction(() =>
      document.querySelector('#mail-state').textContent.includes('응답하지 않습니다'),
    );
    assert.equal(await page.locator('#mail-refresh').isDisabled(), false);
    mode = 'error';
    await page.locator('#mail-refresh').click();
    await page.waitForFunction(() =>
      document.querySelector('#mail-state').textContent.includes('가상 인증 오류'),
    );
    assert.equal(await page.locator('#mail-refresh').isDisabled(), false);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
