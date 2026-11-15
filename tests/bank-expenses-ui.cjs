/** 가상 내역으로 계좌 소비 확인·취소·후보 경고·개인 버전·휴대폰 배치를 검증합니다. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const page = await browser.newPage({ viewport: { width: 375, height: 812 } });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const base = process.env.MOA_TEST_URL;
    const ledger = () => page.evaluate(async () => (await api('/api/data')).transactions);
    const waitBank = async () => {
      await page.waitForFunction(() => !document.querySelector('#bank-review-editor').open);
      await page.selectOption('#bank-review-filter', 'all');
      await page.waitForFunction(() => document.querySelector('[data-bank-review]'));
    };
    const open = async () => {
      await page.locator('[data-bank-review]').click();
      await page.locator('#bank-review-editor').waitFor();
    };
    await page.goto(base + '/login');
    await page.locator('#login-password').fill('fixture-owner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');
    await page.locator('.dashboard-toolbar').waitFor();
    assert.equal(await page.locator('#open-bank-review').isVisible(), false);
    await page.goto(base + '/#bank');
    await page.locator('[data-bank-review]').waitFor();
    assert.equal(await page.locator('#bank-request').isVisible(), false);
    const toggle = page.locator('[data-bank-consumption]');
    assert.equal(await toggle.isChecked(), false);
    await toggle.click();
    await page.locator('#bank-review-editor').waitFor();
    assert.equal(await page.locator('#bank-expense-fields').isVisible(), false);
    assert.equal(await page.locator('#bank-decision-label').isVisible(), false);
    await page.locator('#bank-review-close').click();
    assert.equal(await toggle.isChecked(), false);
    assert.equal((await ledger()).filter((row) => row.payment_kind === 'bank').length, 0);
    await toggle.click();
    await page.locator('#bank-review-editor').waitFor();
    await page.evaluate(() => document.body.classList.add('session-check-pending'));
    assert.equal(await page.locator('#bank-review-editor').isVisible(), false);
    await page.evaluate(() => document.body.classList.remove('session-check-pending'));
    assert.equal(await page.locator('#bank-review-editor').isVisible(), true);
    assert.equal(await page.locator('#bank-candidate-list input').count(), 1);
    await page.locator('#bank-review-save').click();
    assert.equal(await page.locator('#bank-review-editor').isVisible(), true);
    assert.equal((await ledger()).filter((row) => row.payment_kind === 'bank').length, 0);
    await page.locator('#bank-confirm-separate').check();
    await page.locator('#bank-review-save').click();
    await waitBank();
    await page.waitForFunction(
      () => document.querySelector('.bank-decision').textContent === '소비 반영 중',
    );
    let rows = await ledger();
    const expense = rows.find((row) => row.payment_kind === 'bank');
    assert.equal(rows.length, 2);
    assert.equal(expense.merchant, '가상 은행 설명');
    assert.equal(expense.category, '미분류');
    assert.equal(expense.category_reviewed, false);
    assert.equal(await toggle.isChecked(), true);
    // 선택 해제·재선택에는 전체 분류 폼을 요구하지 않습니다.
    await toggle.click();
    await page.waitForFunction(
      () => document.querySelector('.bank-decision').textContent === '소비 미반영',
    );
    await toggle.click();
    await page.locator('#bank-review-editor').waitFor();
    await page.locator('#bank-confirm-separate').check();
    await page.locator('#bank-review-save').click();
    await waitBank();
    await page.waitForFunction(() => document.querySelector('[data-bank-consumption]').checked);
    assert.equal((await ledger()).find((row) => row.payment_kind === 'bank').id, expense.id);
    await open();
    await page.locator('#bank-expense-merchant').fill('가상 계좌 식당');
    await page.selectOption('#bank-expense-category', '식비');
    await page.locator('#bank-expense-memo').fill('가상 개인 메모');
    await page.locator('#bank-confirm-separate').check();
    await page.locator('#bank-review-save').click();
    await waitBank();
    await open();
    for (const width of [320, 375, 430, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      const box = await page.locator('#bank-review-editor').boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width);
      assert.equal(
        await page
          .locator('#bank-review-editor')
          .evaluate((element) => element.scrollWidth > element.clientWidth + 1),
        false,
      );
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
    }
    await page.setViewportSize({ width: 375, height: 812 });
    if (process.env.MOA_SCREENSHOT_DIR) {
      await page.locator('#bank-review-editor').evaluate((element) => {
        element.scrollTop = 0;
      });
      await page.screenshot({
        path: process.env.MOA_SCREENSHOT_DIR + '/bank-expense-review-mobile.png',
      });
    }
    // 다른 화면의 수정이 있어도 백그라운드 목록 조회로 편집 버전을 덮어쓰면 안 됩니다.
    await page.evaluate(async () => {
      const data = await api('/api/data');
      const card = data.transactions.find((row) => row.payment_kind === 'card');
      await api(`/api/transactions/${card.id}/category`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category: '커피', scope: 'transaction' }),
      });
      await loadBank();
    });
    await page.locator('#bank-expense-merchant').fill('가상 수정 중인 사용처');
    await page.locator('#bank-confirm-separate').check();
    await page.locator('#bank-review-save').click();
    await page.waitForFunction(() =>
      document.querySelector('#bank-review-error').textContent.includes('먼저 변경'),
    );
    assert.equal((await ledger()).find((row) => row.id === expense.id).merchant, '가상 계좌 식당');
    await page.locator('#bank-review-reload').click();
    await page.waitForFunction(
      () => document.querySelector('#bank-review-error').textContent === '',
    );
    assert.equal(
      await page.locator('#bank-expense-merchant').inputValue(),
      '가상 수정 중인 사용처',
    );
    assert.equal(await page.locator('#bank-confirm-separate').isChecked(), false);
    await page.selectOption('#bank-review-decision', 'card_duplicate');
    await page.locator('#bank-candidate-list input').check();
    await page.locator('#bank-review-save').click();
    await waitBank();
    await page.waitForFunction(
      () => document.querySelector('.bank-decision').textContent === '카드 내역과 중복',
    );
    rows = await ledger();
    assert.equal(rows.find((row) => row.id === expense.id).excluded, 1);
    assert.equal(
      rows.filter((row) => !row.excluded).reduce((sum, row) => sum + row.amount, 0),
      1200,
    );
    assert.equal(rows.find((row) => row.payment_kind === 'card').category, '커피');
    await open();
    await page.selectOption('#bank-review-decision', 'expense');
    await page.locator('#bank-confirm-separate').check();
    await page.locator('#bank-review-save').click();
    await waitBank();
    await page.waitForFunction(
      () => document.querySelector('.bank-decision').textContent === '소비 반영 중',
    );
    rows = await ledger();
    assert.equal(rows.length, 2);
    assert.equal(rows.find((row) => row.payment_kind === 'bank').id, expense.id);
    await toggle.click();
    await waitBank();
    await page.waitForFunction(
      () => document.querySelector('.bank-decision').textContent === '소비 미반영',
    );
    await page.goto(base + '/#bank');
    await page.reload();
    await page.locator('[data-bank-review]').waitFor();
    assert.equal(
      await page.locator('.mobile-nav a[href="#sharing"]').getAttribute('aria-current'),
      'page',
    );
    if (process.env.MOA_SCREENSHOT_DIR)
      await page.screenshot({
        path: process.env.MOA_SCREENSHOT_DIR + '/bank-expense-list-mobile.png',
      });
    await page.goto(base + '/#dashboard');
    await page.locator('.dashboard-toolbar').waitFor();
    assert.equal(await page.locator('#open-bank-review').isVisible(), false);
    assert.equal((await page.locator('#card-count').textContent()).trim(), '1개');
    // 카드 후보가 없는 출금은 체크 한 번으로 반영하고 해제하며, 낡은 목록은 적용하지 않습니다.
    await page.goto(base + '/#bank');
    await page.locator('#bank-month').fill(process.env.MOA_SIMPLE_BANK_MONTH);
    await page.locator('#bank-month').dispatchEvent('change');
    await page.waitForFunction(() =>
      document.querySelector('#bank-transactions').textContent.includes('가상 간단 선택'),
    );
    await toggle.click();
    await page.waitForFunction(
      () =>
        document.querySelector('[data-bank-consumption]').checked &&
        !document.querySelector('[data-bank-consumption]').disabled,
    );
    assert.equal(await page.locator('#bank-review-editor').isVisible(), false);
    const simple = (await ledger()).find((row) => row.merchant === '가상 간단 선택');
    assert.equal(simple.category, '미분류');
    assert.equal(simple.category_reviewed, false);
    await toggle.click();
    await page.waitForFunction(
      () =>
        !document.querySelector('[data-bank-consumption]').checked &&
        !document.querySelector('[data-bank-consumption]').disabled,
    );
    await page.evaluate(async () => {
      const data = await api('/api/data');
      const card = data.transactions.find((row) => row.payment_kind === 'card');
      await api(`/api/transactions/${card.id}/category`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category: '식비', scope: 'transaction' }),
      });
    });
    await toggle.click();
    await page.waitForFunction(() =>
      document.querySelector('#status').textContent.includes('최신 목록'),
    );
    assert.equal(await toggle.isChecked(), false);
    assert.equal((await ledger()).find((row) => row.id === simple.id).excluded, 1);
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
