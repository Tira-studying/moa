/** 가상 거래·실제 임시 서버로 사용 내역 달력의 금액 표시, 날짜 선택, 월 이동, 필터, 기억, 작은 화면을 검증합니다. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const base = process.env.MOA_TEST_URL,
      current = process.env.MOA_TEST_MONTH,
      previous = process.env.MOA_TEST_PREVIOUS;
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage(),
      errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(base + '/login');
    await page.locator('#login-password').fill('fixture-owner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');
    const day = (date) => page.locator(`[data-calendar-day="${date}"]`);

    // 1. 기본은 목록 보기이고, 달력으로 바꾸면 조회 기간 대신 달력이 나옵니다.
    await page.goto(base + '/#activity');
    await page.waitForFunction(() => document.querySelector('#result-count').textContent === '3건');
    assert.equal(await page.locator('#spending-calendar').isHidden(), true);
    await page.locator('[data-activity-mode="calendar"]').click();
    assert.equal(await page.locator('#spending-calendar').isVisible(), true);
    assert.equal(await page.locator('.activity-period').isHidden(), true);
    assert.equal(
      await page.locator('#calendar-title').textContent(),
      `${current.slice(0, 4)}년 ${Number(current.slice(5))}월`,
    );
    assert.equal(await page.locator('#calendar-next').isDisabled(), true);
    assert.match(await day(`${current}-01`).textContent(), /3\.5만\s*2건/);
    assert.match(await page.locator('#calendar-summary').textContent(), /합계 35,000원/);
    assert.equal(await page.locator('#result-count').textContent(), '2건');

    // 2. 날짜를 누르면 목록이 그날로 좁혀지고, 다시 누르면 한 달 전체로 돌아갑니다.
    await day(`${current}-01`).click();
    assert.equal(await day(`${current}-01`).getAttribute('aria-pressed'), 'true');
    assert.match(
      await page.locator('#tx-summary').textContent(),
      new RegExp(`^${current}-01 · 2건`),
    );
    assert.equal(await page.locator('#calendar-clear-day').isVisible(), true);
    await day(`${current}-01`).click();
    assert.equal(await page.locator('#calendar-clear-day').isHidden(), true);
    assert.match(await page.locator('#tx-summary').textContent(), new RegExp(`^${current} · 2건`));

    // 3. 지난달: 첫 기록(10일) 전은 무지출로 세지 않고, 이후 빈 날은 무지출입니다.
    await page.locator('#calendar-prev').click();
    assert.equal(await page.locator('#result-count').textContent(), '1건');
    assert.match(await day(`${previous}-10`).textContent(), /1\.2만/);
    assert.doesNotMatch(await day(`${previous}-09`).textContent(), /무지출/);
    assert.match(await day(`${previous}-11`).textContent(), /무지출/);
    assert.equal(
      await day(`${previous}-10`)
        .getAttribute('class')
        .then((c) => c.includes('level-4')),
      true,
    );

    // 4. 검색 조건이 달력 금액에도 적용됩니다.
    await page.locator('#calendar-next').click();
    await page.locator('#search').fill('카페');
    await page.waitForFunction(
      (date) => /5,000/.test(document.querySelector(`[data-calendar-day="${date}"]`).textContent),
      `${current}-01`,
    );
    await page.locator('#search').fill('');

    // 5. 고른 보기 방식은 새로고침 후에도 유지됩니다.
    await page.reload();
    await page.waitForFunction(() => !document.querySelector('#spending-calendar').hidden);
    await page.waitForFunction(
      (date) => /3\.5만/.test(document.querySelector(`[data-calendar-day="${date}"]`).textContent),
      `${current}-01`,
    );

    // 6. 작은 화면에서 7칸이 가로로 넘치지 않아야 합니다.
    await page.setViewportSize({ width: 375, height: 812 });
    await page.waitForTimeout(200);
    assert.ok(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1),
    );
    const box = await day(`${current}-01`).boundingBox();
    assert.ok(box.width >= 30, `날짜 칸 너비 ${box.width}`);

    // 7. 목록으로 돌아가면 달력이 숨고 조회 기간이 다시 보입니다.
    await page.locator('[data-activity-mode="list"]').click();
    assert.equal(await page.locator('#spending-calendar').isHidden(), true);
    assert.equal(await page.locator('.activity-period').isVisible(), true);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
