/** 가상 두 계정으로 사용 내역 '함께 보기', 거래별 숨기기, 소비에서 제외를 실제 화면에서 확인합니다. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const base = process.env.MOA_TEST_URL;
    const errors = [];
    const open = async (name, password) => {
      const page = await (
        await browser.newContext({ viewport: { width: 375, height: 812 } })
      ).newPage();
      page.on('pageerror', (error) => errors.push(error.message));
      await page.goto(base + '/login');
      if (name) await page.locator('#login-username').fill(name);
      await page.locator('#login-password').fill(password);
      await page.locator('#login-button').click();
      await page.waitForURL(base + '/');
      return page;
    };
    const owner = await open('', 'fixture-owner-password');
    const partner = await open('partner', 'fixture-partner-password');
    const sharedText = () => owner.locator('#shared-activity-list').textContent();
    const showShared = async () => {
      await owner.goto(base + '/#activity');
      // 이전에 그린 목록을 비워 새로 받은 내용만 확인합니다.
      await owner.evaluate(() => (document.querySelector('#shared-activity-list').innerHTML = ''));
      await owner.locator('[data-activity-scope="shared"]').click();
      await owner.locator('#shared-activity-month').fill('2026-11');
      await owner.locator('#shared-activity-month').dispatchEvent('change');
      await owner.waitForFunction(() =>
        document.querySelector('#shared-activity-list').textContent.includes('partner'),
      );
    };

    // 1. 기본으로 함께 보기가 켜져 있어 상대 내역과 메모가 보이고, 내 필터·목록은 가려집니다.
    await showShared();
    assert.match(await sharedText(), /가상 동네빵집[\s\S]*PRIVATE_MEMO/);
    assert.match(await sharedText(), /partner10,500원3건/);
    // 두 사람 내역을 한 목록에 날짜순으로 합치고, 각 줄에 누구의 거래인지 표시합니다.
    const order = await owner.evaluate(() =>
      [...document.querySelectorAll('#shared-activity-list .shared-activity-row')].map(
        (row) =>
          row.querySelector('.shared-who').textContent +
          ':' +
          row.querySelector('strong').textContent,
      ),
    );
    assert.deepEqual(order, [
      'partner:가상환불',
      'partner:가상시장',
      'owner:소유자가게',
      'partner:가상 동네빵집',
    ]);
    assert.match(await sharedText(), /삼성 ·9876/);
    assert.equal(await owner.locator('#transactions').isVisible(), false);
    assert.equal(
      await owner.evaluate(() => document.documentElement.scrollWidth > innerWidth),
      false,
    );
    if (process.env.MOA_SCREENSHOT_DIR)
      await owner.screenshot({ path: process.env.MOA_SCREENSHOT_DIR + '/shared-activity.png' });
    // 상대 거래에 붙은 영수증 사진은 함께 보기에서 눌러 크게 볼 수 있습니다.
    await owner.locator('[data-shared-receipts]').click();
    await owner.locator('#receipt-viewer-images img[src^="data:image/jpeg"]').waitFor();
    assert.equal(await owner.locator('#receipt-viewer').isVisible(), true);
    await owner.locator('#receipt-viewer-close').click();
    assert.equal(await owner.locator('#receipt-viewer').isVisible(), false);
    await owner.locator('[data-activity-scope="mine"]').click();
    // 내 내역·함께 보기와 목록·달력 버튼은 한 줄에서 같은 높이입니다.
    const heights = await owner.evaluate(() =>
      [...document.querySelectorAll('[data-activity-scope], [data-activity-mode]')].map((button) =>
        Math.round(button.getBoundingClientRect().height),
      ),
    );
    assert.equal(new Set(heights).size, 1, JSON.stringify(heights));
    assert.equal(await owner.locator('#activity-shared').isHidden(), true);

    // 2. 상대가 거래 상세에서 이 거래만 숨기면 함께 보기에서 빠집니다.
    await partner.goto(base + '/#activity');
    await partner.locator('[data-transaction-details="101"]').click();
    assert.equal(await partner.locator('#transaction-shared-hidden').isChecked(), false);
    await partner.locator('#transaction-shared-hidden').check();
    if (process.env.MOA_SCREENSHOT_DIR)
      await partner
        .locator('#transaction-editor')
        .screenshot({ path: process.env.MOA_SCREENSHOT_DIR + '/transaction-flags.png' });
    await partner.locator('#transaction-edit-save').click();
    await partner.waitForFunction(() =>
      document.querySelector('#transactions').textContent.includes('상대에게 숨김'),
    );
    await showShared();
    assert.equal((await sharedText()).includes('가상 동네빵집'), false);

    // 3. 소비에서 제외하면 본인 집계 목록에서 빠지고 '제외한 내역'에서 소비 제외로 보입니다.
    await partner.locator('[data-transaction-details="105"]').click();
    await partner.locator('#transaction-not-spending').check();
    await partner.locator('#transaction-edit-save').click();
    await partner.waitForFunction(
      () => !document.querySelector('#transactions').textContent.includes('가상환불'),
    );
    // 휴대폰 화면에서는 필터를 펼쳐야 집계 포함 여부를 고를 수 있습니다.
    await partner.locator('#activity-filter-toggle').click();
    await partner.locator('#tx-visibility').selectOption('excluded');
    await partner.waitForFunction(() =>
      document.querySelector('#transactions').textContent.includes('소비 제외'),
    );
    await showShared();
    assert.equal((await sharedText()).includes('가상환불'), false);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
