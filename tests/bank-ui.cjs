/** 가상 은행 내역으로 딥링크·모바일 금액 표시·소유권·수집 중지를 검증합니다. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const context = await browser.newContext({ viewport: { width: 375, height: 812 } });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const base = process.env.MOA_TEST_URL;
    await page.goto(base + '/login');
    await page.locator('#login-password').fill('fixture-owner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');
    await page.goto(base + '/#bank');
    await page.locator('.bank-transaction').first().waitFor();
    assert.match(await page.locator('#bank-state').textContent(), /00·08·14·20시/);
    const amounts = page.locator('.bank-amount');
    assert.equal(await amounts.count(), 2);
    assert.match((await amounts.allTextContents()).join('\n'), /출금 −1,200원/);
    assert.match((await amounts.allTextContents()).join('\n'), /금액 변동 없음/);
    assert.doesNotMatch((await amounts.allTextContents()).join('\n'), /출금 −0원/);
    assert.equal(await page.locator('[data-view]:visible').count(), 1);
    for (const width of [320, 375, 430, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
      for (const amount of await amounts.all()) {
        const box = await amount.boundingBox();
        assert.ok(box.x >= 0 && box.x + box.width <= width);
      }
    }
    await page.setViewportSize({ width: 375, height: 812 });
    if (process.env.MOA_SCREENSHOT_DIR)
      await page.screenshot({
        path: process.env.MOA_SCREENSHOT_DIR + '/bank-mobile-fixture.png',
        fullPage: true,
      });
    if (!(await page.locator('#bank-connection-details').evaluate((element) => element.open)))
      await page.locator('#bank-connection-details summary').click();
    await page.locator('#bank-links button').click();
    await page.waitForFunction(
      () =>
        document.querySelector('#bank-links').textContent.includes('수집 중지') &&
        !document.querySelector('#bank-links button'),
    );
    assert.equal(await page.locator('#bank-request').isDisabled(), true);
    await page.locator('#bank a[href="#sharing"]').click();
    await page.locator('#sharing a[href="#bank"]').click();
    await page.locator('.bank-transaction').first().waitFor();
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
