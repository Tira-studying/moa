/** 가상 거래·실제 임시 서버로 카드 실적 추가, 끝자리 구분, 명세서 연결, 대시보드 요약과 작은 화면을 검증합니다. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const base = process.env.MOA_TEST_URL,
      month = process.env.MOA_TEST_MONTH;
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage(),
      errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('dialog', (dialog) => dialog.accept());
    await page.goto(base + '/login');
    await page.locator('#login-password').fill('fixture-owner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');

    // 0. 기본은 꺼짐: 탭과 대시보드 요약이 없고, 설정에서 켭니다.
    await page.waitForFunction(
      () => document.querySelector('[data-finance-tab="performance"]').hidden,
    );
    assert.equal(await page.locator('#dashboard-card-performance').isHidden(), true);
    await page.goto(base + '/#sharing');
    await page.locator('#feature-settings summary').click();
    assert.equal(await page.locator('#card-performance-enabled').isChecked(), false);
    await page.locator('#card-performance-enabled').check();
    await page.waitForFunction(
      () => !document.querySelector('[data-finance-tab="performance"]').hidden,
    );

    // 1. 지출 관리 → 카드 실적 탭, 추천 값으로 카드 추가
    await page.goto(base + '/#planning');
    await page.locator('#finance-month').fill(month);
    await page.locator('#finance-month').dispatchEvent('change');
    await page.locator('[data-finance-tab="performance"]').click();
    await page.waitForFunction(() =>
      document.querySelector('#card-performance-state').textContent.includes('등록한 카드가 없습니다'),
    );
    await page.locator('#card-product-editor summary').click();
    await page.locator('#card-product-preset').selectOption({ label: '우리 · 위비트래블 체크' });
    assert.equal(await page.locator('#card-product-form [name="name"]').inputValue(), '위비트래블 체크');
    await page.locator('#card-product-form [name="last_fours"]').fill('1111');
    await page.locator('#card-product-save').click();
    await page.waitForFunction(() =>
      document.querySelector('#card-performance-list').textContent.includes('위비트래블 체크'),
    );
    const first = page.locator('.card-performance-item').first();
    // 우리카드가 한 장뿐이면 끝자리 없는 명세서 거래(1,500원)도 이 카드로 봅니다. 다른 끝자리(2222)는 제외합니다.
    assert.match(await first.textContent(), /이번 달 인정 실적\s*5,500원/);
    assert.match(await first.textContent(), /캐시백 1구간까지 194,500원 남음/);
    assert.match(await page.locator('#card-performance-unassigned').textContent(), /끝자리\s*2222/);

    // 2. 같은 카드사 두 번째 카드: 끝자리로 나뉘고, 끝자리 없는 명세서는 카드에 연결
    await page.locator('#card-product-form [name="issuer"]').selectOption('우리');
    await page.locator('#card-product-form [name="name"]').fill('가상 생활 카드');
    await page.locator('#card-product-form [name="last_fours"]').fill('2222');
    await page.locator('#card-product-form [name="benefits"]').fill('할인, 300,000원');
    await page.locator('#card-product-save').click();
    await page.waitForFunction(
      () =>
        document.querySelectorAll('.card-performance-item').length === 2 ||
        document.querySelector('#card-product-error').textContent,
    );
    assert.equal(await page.locator('#card-product-error').textContent(), '');
    assert.equal(await page.locator('#card-performance-statements').isHidden(), false);
    // 두 장이 되면 끝자리 없는 명세서 거래는 어느 카드인지 알 수 없어 따로 보여 줍니다.
    assert.match(await page.locator('#card-performance-unassigned').textContent(), /끝자리 없음/);
    const link = page.locator('[data-statement-card]').first();
    const firstId = await page.locator('[data-card-edit]').first().getAttribute('data-card-edit');
    await link.selectOption(firstId);
    await page.waitForFunction(() => document.querySelector('#card-performance-unassigned').hidden);
    assert.match(await page.locator('.card-performance-item').first().textContent(), /5,500원/);

    // 3. 잘못된 혜택 줄은 저장하지 않고 안내합니다.
    await page.locator('#card-product-form [name="name"]').fill('잘못된 입력');
    await page.locator('#card-product-form [name="benefits"]').fill('금액 없음');
    await page.locator('#card-product-save').click();
    assert.match(await page.locator('#card-product-error').textContent(), /혜택 줄을 확인/);
    await page.locator('#card-product-reset').click();

    // 4. 대시보드 요약과 관리 이동
    await page.goto(base + '/#dashboard');
    await page.waitForFunction(() => !document.querySelector('#dashboard-card-performance').hidden);
    assert.match(
      await page.locator('#dashboard-card-performance').textContent(),
      /위비트래블 체크[\s\S]*캐시백 1구간까지/,
    );
    await page.locator('#card-performance-open').click();
    await page.waitForFunction(
      () => !document.querySelector('[data-finance-panel="performance"]').hidden,
    );

    // 5. 끄면 보던 탭에서 기본 탭으로 돌아가고 대시보드 요약도 숨깁니다. 다시 켜면 카드가 그대로 있습니다.
    await page.goto(base + '/#sharing');
    await page.locator('#card-performance-enabled').uncheck();
    await page.waitForFunction(
      () => document.querySelector('[data-finance-tab="performance"]').hidden,
    );
    await page.goto(base + '/#dashboard');
    await page.waitForTimeout(500);
    assert.equal(await page.locator('#dashboard-card-performance').isHidden(), true);
    await page.goto(base + '/#sharing');
    await page.locator('#card-performance-enabled').check();
    await page.waitForFunction(
      () => !document.querySelector('[data-finance-tab="performance"]').hidden,
    );

    // 6. 작은 화면에서 가로로 넘치지 않아야 합니다.
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(base + '/#dashboard');
    await page.waitForFunction(() => !document.querySelector('#dashboard-card-performance').hidden);
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
