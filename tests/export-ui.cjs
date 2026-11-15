/** 가상 거래·실제 임시 서버로 사용 내역의 [엑셀 내려받기]가 화면 조건대로 파일을 받는지 검증합니다. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const base = process.env.MOA_TEST_URL;
    const context = await browser.newContext({
      viewport: { width: 1280, height: 900 },
      acceptDownloads: true,
    });
    const page = await context.newPage(),
      errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(base + '/login');
    await page.locator('#login-password').fill('fixture-owner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');
    await page.goto(base + '/#activity');
    await page.waitForFunction(() => document.querySelector('#result-count').textContent === '4건');

    // 1. 전체 기간 목록 그대로 내려받습니다.
    let [download] = await Promise.all([
      page.waitForEvent('download'),
      page.locator('#export-download').click(),
    ]);
    assert.equal(download.suggestedFilename(), '모아-사용내역-전체기간.xlsx');
    const size = fs.statSync(await download.path()).size;
    assert.ok(size > 4000, `파일 크기 ${size}`);

    // 2. 달력에서 날짜를 고르면 그 날짜 파일이 됩니다.
    await page.evaluate(() => ($('tx-month').value = '2026-11'));
    await page.locator('[data-activity-mode="calendar"]').click();
    await page.locator('[data-calendar-day="2026-11-03"]').click();
    [download] = await Promise.all([
      page.waitForEvent('download'),
      page.locator('#export-download').click(),
    ]);
    assert.equal(download.suggestedFilename(), '모아-사용내역-2026-11-03.xlsx');

    // 3. 잘못된 기간이면 서버 안내를 보여 줍니다.
    await page.locator('[data-activity-mode="list"]').click();
    await page.locator('#tx-period').selectOption('custom');
    await page.locator('#tx-start').fill('2026-11-05');
    await page.locator('#tx-end').fill('2026-11-01');
    await page.locator('#export-download').click();
    await page.waitForFunction(() =>
      document.querySelector('#status').textContent.includes('시작일은 종료일보다'),
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
