/** 가상 공동관리자로 휴대폰의 카드·계좌 인증, SMS 복귀·취소·비밀 미보관을 확인합니다. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const page = await browser.newPage({ viewport: { width: 375, height: 812 } });
    const base = process.env.MOA_TEST_URL;
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(base + '/login');
    await page.locator('#login-username').fill('partner');
    await page.locator('#login-password').fill('fixture-partner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');
    await page.goto(base + '/#sharing');
    await page.locator('#sharing a[href="#connections"]').first().click();
    await page.waitForFunction(() => !document.querySelector('#connection-inputs').disabled);
    assert.equal(
      await page.locator('.mobile-nav a[href="#sharing"]').getAttribute('aria-current'),
      'page',
    );
    for (const width of [320, 375, 430, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      for (const provider of ['samsung', 'hyundai', 'lotte']) {
        await page.selectOption('#connection-provider', provider);
        assert.equal(
          await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
          false,
        );
      }
    }
    await page.setViewportSize({ width: 375, height: 812 });
    await page.selectOption('#connection-provider', 'samsung');
    // 아이디는 오타를 확인할 수 있게 보이고, 비밀번호만 가립니다.
    assert.equal(await page.locator('#connection-login-id').getAttribute('type'), 'text');
    assert.equal(await page.locator('#connection-password').getAttribute('type'), 'password');
    await page.locator('#connection-login-id').fill('fixture-web-login');
    await page.locator('#connection-password').fill('fixture-web-password');
    await page.locator('#connection-consent').check();
    await page.locator('#connection-start').click();
    await page.waitForFunction(() => !document.querySelector('#connection-challenge-form').hidden);
    assert.equal(await page.locator('#connection-password').inputValue(), '');
    assert.equal(await page.locator('#connection-login-id').inputValue(), '');
    // SMS 앱으로 이동했다 돌아와도 새 로그인을 보내지 않고 같은 추가 인증을 복원합니다.
    const first = await page.evaluate(() => connectionJob.id);
    await page.reload();
    await page.waitForFunction(() => !document.querySelector('#connection-challenge-form').hidden);
    assert.equal(await page.evaluate(() => connectionJob.id), first);
    await page.locator('#connection-code').fill('123456');
    await page.locator('#connection-answer').click();
    await page.waitForFunction(
      () =>
        connectionJob?.state === 'success' &&
        !document.querySelector('#connection-inputs').disabled,
    );
    assert.equal(await page.locator('#connection-code').inputValue(), '');
    const ledger = await page.evaluate(async () => (await api('/api/data')).transactions);
    assert.equal(ledger.length, 1);
    assert.equal(ledger[0].card, '삼성');
    await page.locator('#connection-new').click();
    await page.locator('#connection-form').waitFor();
    // 은행 필드는 같은 화면에서 동적으로 표시하며 8자리 생년월일만 받습니다.
    await page.selectOption('#connection-kind', 'bank');
    assert.equal(await page.locator('#connection-bank').isVisible(), true);
    assert.equal(await page.locator('#connection-hyundai').isVisible(), false);
    await page.locator('#connection-login-id').fill('fixture-bank-login');
    await page.locator('#connection-password').fill('fixture-bank-password');
    await page.locator('#connection-birth').fill('19900101');
    await page.locator('#connection-account').fill('1002000000000');
    await page.locator('#connection-account-pin').fill('0000');
    await page.locator('#connection-consent').check();
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
      false,
    );
    if (process.env.MOA_SCREENSHOT_DIR) {
      // QA 이미지에도 입력을 남기지 않습니다.
      await page.evaluate(() => clearConnectionInputs());
      await page.screenshot({
        path: process.env.MOA_SCREENSHOT_DIR + '/phone-bank-connection.png',
        fullPage: true,
      });
      await page.locator('#connection-login-id').fill('fixture-bank-login');
      await page.locator('#connection-password').fill('fixture-bank-password');
      await page.locator('#connection-birth').fill('19900101');
      await page.locator('#connection-account').fill('1002000000000');
      await page.locator('#connection-account-pin').fill('0000');
      await page.locator('#connection-consent').check();
    }
    await page.locator('#connection-start').click();
    await page.waitForFunction(() => connectionJob?.state === 'needs_input');
    await page.locator('#connection-code').fill('654321');
    await page.locator('#connection-cancel').click();
    await page.waitForFunction(() => connectionJob?.state === 'cancelled');
    assert.equal(await page.locator('#connection-code').inputValue(), '');
    assert.equal(await page.evaluate(async () => (await api('/api/bank')).links.length), 0);
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
