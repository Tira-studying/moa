/** 가상 거래·실제 임시 서버로 예산·연말정산·정기 결제의 켜기·끄기, 입력, 대시보드 요약과 작은 화면을 검증합니다. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const base = process.env.MOA_TEST_URL;
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage(),
      errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(base + '/login');
    await page.locator('#login-password').fill('fixture-owner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');

    // 0. 기본은 모두 꺼짐: 탭·요약이 없습니다.
    await page.waitForTimeout(500);
    for (const tab of ['budgets', 'tax'])
      assert.equal(await page.locator(`[data-finance-tab="${tab}"]`).isHidden(), true);
    for (const id of ['dashboard-budget', 'dashboard-tax', 'dashboard-subscriptions'])
      assert.equal(await page.locator('#' + id).isHidden(), true);

    // 1. 설정에서 세 기능을 켭니다.
    await page.goto(base + '/#sharing');
    await page.locator('#feature-settings summary').click();
    for (const feature of ['budgets', 'tax_deduction', 'subscriptions']) {
      await page.locator(`[data-feature="${feature}"]`).check();
      await page.waitForFunction(
        (name) => !document.querySelector(`[data-feature="${name}"]`).disabled,
        feature,
      );
    }
    await page.goto(base + '/#dashboard');
    await page.waitForFunction(() => !document.querySelector('#dashboard-tax').hidden);
    assert.match(await page.locator('#dashboard-tax').textContent(), /총급여를 입력하면/);
    await page.waitForFunction(() => !document.querySelector('#dashboard-subscriptions').hidden);
    assert.match(await page.locator('#dashboard-subscriptions').textContent(), /정기 결제 1건/);
    assert.equal(await page.locator('#dashboard-budget').isHidden(), true);

    // 2. 예산: 식비 5만원을 정하면 8만원 사용으로 초과 알림이 뜹니다.
    await page.locator('[data-open-tab="budgets"]').evaluate((button) => button.click());
    await page.waitForFunction(() => document.querySelector('[data-budget="식비"]'));
    await page.locator('[data-budget="식비"]').fill('50,000');
    await page.locator('#budget-save').click();
    await page.waitForFunction(() =>
      document.querySelector('#budget-list').textContent.includes('초과'),
    );
    await page.goto(base + '/#dashboard');
    await page.waitForFunction(() => !document.querySelector('#open-budget-alert').hidden);
    assert.match(await page.locator('#budget-alert-summary').textContent(), /예산 초과 1개 분야/);

    // 3. 연말정산: 총급여를 넣으면 문턱과 예상 공제액을 보여 줍니다.
    await page.locator('[data-open-tab="tax"]').click();
    await page.waitForFunction(() => document.querySelector('#tax-form [name="salary"]'));
    await page.waitForFunction(() => document.querySelector('[data-tax-issuer]'));
    await page.locator('#tax-form [name="salary"]').fill('40,000,000');
    await page.locator('#tax-save').click();
    await page.waitForFunction(() =>
      document.querySelector('#tax-result').textContent.includes('10,000,000원'),
    );
    assert.match(await page.locator('#tax-result').textContent(), /공제 문턱까지/);
    assert.equal(await page.locator('#tax-error').textContent(), '');

    // 4. 고정 지출 탭 위의 구독 요약
    await page.locator('[data-finance-tab="recurring"]').click();
    await page.waitForFunction(() => !document.querySelector('#subscription-summary').hidden);
    assert.match(await page.locator('#subscription-summary').textContent(), /가상OTT/);

    // 5. 예산을 끄면 탭·알림이 사라집니다.
    await page.goto(base + '/#sharing');
    await page.locator('[data-feature="budgets"]').uncheck();
    await page.waitForFunction(
      () => document.querySelector('[data-finance-tab="budgets"]').hidden,
    );
    await page.goto(base + '/#dashboard');
    await page.waitForTimeout(500);
    assert.equal(await page.locator('#open-budget-alert').isHidden(), true);
    assert.equal(await page.locator('#dashboard-budget').isHidden(), true);

    // 6. 작은 화면에서 가로로 넘치지 않아야 합니다.
    await page.setViewportSize({ width: 375, height: 812 });
    await page.reload();
    await page.waitForFunction(() => !document.querySelector('#dashboard-tax').hidden);
    assert.ok(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1),
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
