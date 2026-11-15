/** 가상 원장으로 모바일·PC 개인 분야 편집과 기존 거래 분류·그래프 연결을 검증합니다. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const base = process.env.MOA_TEST_URL;
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(base + '/login');
    await page.locator('#login-password').fill('fixture-owner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');
    await page.waitForFunction(() => document.querySelector('#tx-count').textContent.includes('3'));
    await page.locator('#manage-categories').click();
    const formFor = (name) =>
      page.locator('#category-settings-list form').filter({
        has: page.getByRole('textbox', { name: name + ' 이름', exact: true }),
      });
    await formFor('육아').waitFor();
    assert.equal(await formFor('구독료').count(), 1);
    const saved = () =>
      page.waitForFunction(
        () =>
          document.querySelector('#category-manager-state').textContent ===
          '내 소비 분야를 저장했습니다.',
      );
    const add = async (name) => {
      await page.locator('#category-new-name').fill(name);
      await page.locator('#category-create-form button').click();
      await saved();
    };
    await add('반려동물');
    let pet = formFor('반려동물');
    await pet.locator('input').fill('동물 돌봄');
    await pet.locator('button[type="submit"]').click();
    await saved();
    await formFor('육아').locator('input').fill('아이 돌봄');
    await formFor('육아').locator('button[type="submit"]').click();
    await saved();
    await add('가상 임시 분야');
    page.once('dialog', (dialog) => dialog.accept());
    await formFor('가상 임시 분야').getByRole('button', { name: '삭제', exact: true }).click();
    await saved();
    assert.equal(await formFor('가상 임시 분야').count(), 0);
    // 중복 오류는 입력을 보존하고 다른 분야를 덮어쓰지 않습니다.
    await page.locator('#category-new-name').fill('구독료');
    await page.locator('#category-create-form button').click();
    await page.waitForFunction(() =>
      document.querySelector('#category-manager-state').textContent.includes('이미 사용'),
    );
    assert.equal(await page.locator('#category-new-name').inputValue(), '구독료');
    assert.equal(await formFor('구독료').count(), 1);
    for (const width of [320, 390, 768, 1280]) {
      await page.setViewportSize({ width, height: 844 });
      assert.equal(
        await page
          .locator('#category-manager')
          .evaluate((dialog) => dialog.scrollWidth > dialog.clientWidth),
        false,
      );
    }
    if (process.env.MOA_SCREENSHOT_DIR)
      await page.screenshot({ path: process.env.MOA_SCREENSHOT_DIR + '/categories.png' });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#category-manager-close').click();
    await page.locator('.mobile-nav a[href="#activity"]').click();
    await page.locator('[data-category-id="1"]').selectOption({ label: '동물 돌봄' });
    await page.waitForFunction(
      () =>
        document.querySelector('[data-category-id="1"]').value === '동물 돌봄' &&
        !document.querySelector('[data-category-id="1"]').disabled,
    );
    assert.equal(await page.locator('[data-category-id="2"]').inputValue(), '아이 돌봄');
    await page.locator('.mobile-nav a[href="#dashboard"]').click();
    assert.match(await page.locator('#pattern-distribution').textContent(), /동물 돌봄/);
    await page.locator('#manage-categories').click();
    await formFor('동물 돌봄').waitFor();
    assert.equal(
      await formFor('동물 돌봄').getByRole('button', { name: '삭제', exact: true }).count(),
      0,
    );
    await formFor('동물 돌봄').locator('input').fill('우리집 반려생활');
    await formFor('동물 돌봄').locator('button[type="submit"]').click();
    await saved();
    await page.locator('#category-manager-close').click();
    await page.reload();
    await page.waitForFunction(() =>
      document.querySelector('#pattern-distribution').textContent.includes('우리집 반려생활'),
    );
    await page.locator('.mobile-nav a[href="#activity"]').click();
    assert.equal(await page.locator('[data-category-id="1"]').inputValue(), '우리집 반려생활');
    const partner = await browser.newContext();
    const other = await partner.newPage();
    await other.goto(base + '/login');
    await other.locator('#login-username').fill('partner');
    await other.locator('#login-password').fill('fixture-partner-password');
    await other.locator('#login-button').click();
    await other.waitForURL(base + '/');
    await other.locator('#manage-categories').click();
    await other.getByRole('textbox', { name: '육아 이름', exact: true }).waitFor();
    assert.equal(
      await other.getByRole('textbox', { name: '우리집 반려생활 이름', exact: true }).count(),
      0,
    );
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.deepEqual(errors, []);
    await partner.close();
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
