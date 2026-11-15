/** 임시 서버·가상 키로 소유자의 개발자 인증 확인 화면과 비노출·재요청 제한을 검증합니다. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const base = process.env.MOA_TEST_URL;
    await page.goto(base + '/login');
    await page.locator('#login-password').fill('fixture-owner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');
    await page.locator('.mobile-nav a[href="#sharing"]').click();
    await page.locator('#codef-settings summary').click();
    await page.waitForFunction(() => !document.querySelector('#codef-check').disabled);
    await page.locator('#codef-check').click();
    await page.waitForFunction(() =>
      document.querySelector('#codef-state').textContent.includes('토큰 발급에 성공'),
    );
    const text = await page.locator('#codef-settings').textContent();
    assert.match(text, /카드사 연결·데모 승인내역 조회 권한은 다음 단계/);
    assert.equal(text.includes('fixture-codef-secret'), false);
    assert.equal(await page.locator('#codef-settings input,#codef-settings textarea').count(), 0);
    await page.locator('#codef-check').click();
    await page.waitForFunction(() =>
      document.querySelector('#codef-state').textContent.includes('1분에 한 번'),
    );
    for (const width of [320, 390, 768, 1280]) {
      await page.setViewportSize({ width, height: 844 });
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
    }
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
