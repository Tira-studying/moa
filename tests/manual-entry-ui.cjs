/** 가상 지출·실제 임시 서버로 직접 입력 창의 추가·수정·삭제, 달력 날짜 연동, 작은 화면을 검증합니다. */
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
    page.on('dialog', (dialog) => dialog.accept());
    await page.goto(base + '/login');
    await page.locator('#login-password').fill('fixture-owner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');
    const today = await page.evaluate(() => MoaAnalytics.todayKST());

    // 1. 사용 내역에서 새 지출을 적습니다. 금액은 쉼표를 붙여 보여 줍니다.
    await page.goto(base + '/#activity');
    await page.locator('#activity [data-manual-open]').click();
    assert.equal(await page.locator('#manual-entry').isVisible(), true);
    assert.equal(await page.locator('#manual-date').inputValue(), today);
    await page.locator('#manual-merchant').fill('가상 반찬가게');
    await page.locator('#manual-amount').fill('12000원');
    assert.equal(await page.locator('#manual-amount').inputValue(), '12,000');
    await page.locator('#manual-receipt').check();
    await page.locator('#manual-entry-save').click();
    await page.waitForFunction(() => document.querySelector('#result-count').textContent === '1건');
    assert.equal(await page.locator('#manual-entry').isVisible(), false);
    assert.match(
      await page.locator('#transactions').textContent(),
      /가상 반찬가게[\s\S]*현금[\s\S]*12,000원/,
    );

    // 2. 지역화폐·온누리상품권·기타 결제는 현금영수증 칸이 숨습니다. 빈 금액은 브라우저 필수 입력 검사로 막습니다.
    await page.locator('#activity [data-manual-open]').click();
    for (const method of ['local', 'onnuri', 'other']) {
      await page.locator('#manual-method').selectOption(method);
      assert.equal(await page.locator('.manual-receipt').isHidden(), true, method);
    }
    await page.locator('#manual-method').selectOption('cash');
    assert.equal(await page.locator('.manual-receipt').isHidden(), false);
    await page.locator('#manual-merchant').fill('가상 상품권');
    await page.locator('#manual-entry-save').click();
    assert.equal(await page.locator('#manual-entry').isVisible(), true);
    assert.equal(
      await page.locator('#manual-amount').evaluate((input) => input.validity.valueMissing),
      true,
    );
    await page.locator('#manual-entry-close').click();

    // 3. 행의 '수정'으로 금액을 고치면 목록이 바뀝니다.
    await page.locator('[data-manual-edit]').click();
    assert.equal(await page.locator('#manual-entry-title').textContent(), '직접 입력 수정');
    assert.equal(await page.locator('#manual-receipt').isChecked(), true);
    await page.locator('#manual-amount').fill('15000');
    await page.locator('#manual-entry-save').click();
    await page.waitForFunction(() =>
      document.querySelector('#transactions').textContent.includes('15,000원'),
    );

    // 4. 달력에서 날짜를 고른 뒤 열면 그 날짜로 시작합니다.
    await page.locator('[data-activity-mode="calendar"]').click();
    const first = today.slice(0, 8) + '01';
    await page.locator(`[data-calendar-day="${first}"]`).click();
    await page.locator('#activity [data-manual-open]').click();
    assert.equal(await page.locator('#manual-date').inputValue(), first);
    await page.locator('#manual-merchant').fill('가상 경조사비');
    await page.locator('#manual-amount').fill('50000');
    await page.locator('#manual-method').selectOption('transfer');
    await page.locator('#manual-entry-save').click();
    await page.waitForFunction(
      (date) => /5만/.test(document.querySelector(`[data-calendar-day="${date}"]`).textContent),
      first,
    );
    await page.locator('[data-activity-mode="list"]').click();

    // 5. 삭제하면 목록에서 사라집니다.
    await page.waitForFunction(() => document.querySelector('#result-count').textContent === '2건');
    await page.locator('[data-manual-edit]').first().click();
    await page.locator('#manual-entry-delete').click();
    await page.waitForFunction(() => document.querySelector('#result-count').textContent === '1건');

    // 6. 작은 화면: 창이 가로로 넘치지 않습니다. 파일 추가 화면에서도 열 수 있습니다.
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(base + '/#upload');
    await page.locator('#upload [data-manual-open]').click();
    const box = await page.locator('#manual-entry').boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 375, JSON.stringify(box));
    assert.ok(
      await page.evaluate(() => {
        const form = document.querySelector('#manual-entry-form');
        return form.scrollWidth <= form.clientWidth + 1;
      }),
    );
    await page.locator('#manual-entry-close').click();

    // 7. 첫 화면에서도 현금 지출을 바로 적을 수 있습니다. 작은 화면에서는 범위 버튼 옆에 둡니다.
    await page.goto(base + '/#dashboard');
    const cash = page.locator('#dashboard .dashboard-cash');
    assert.equal(await cash.isVisible(), true);
    const cashBox = await cash.boundingBox();
    assert.ok(cashBox.x >= 0 && cashBox.x + cashBox.width <= 375, JSON.stringify(cashBox));
    await cash.click();
    assert.equal(await page.locator('#manual-entry').isVisible(), true);
    assert.equal(await page.locator('#manual-method').inputValue(), 'cash');
    assert.equal(await page.locator('#manual-date').inputValue(), today);
    await page.locator('#manual-merchant').fill('가상 붕어빵');
    await page.locator('#manual-amount').fill('3000');
    await page.locator('#manual-entry-save').click();
    await page.waitForFunction(() => !document.querySelector('#manual-entry').open);
    await page.goto(base + '/#activity');
    await page.waitForFunction(() =>
      document.querySelector('#transactions').textContent.includes('가상 붕어빵'),
    );
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(base + '/#dashboard');
    assert.equal(await page.locator('#dashboard .dashboard-cash').isVisible(), true);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
