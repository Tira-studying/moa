/**
 * 실제 웹 서버에 두 브라우저를 연결해 계정·충돌·접근 해제를 검증합니다.
 * 실제 거래·암호는 사용하지 않습니다. 브라우저 테스트는 Chromium 샌드박스를 켜고 실행합니다.
 */

const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const owner = await (
        await browser.newContext({ viewport: { width: 390, height: 844 } })
      ).newPage(),
      manager = await (
        await browser.newContext({ viewport: { width: 390, height: 844 } })
      ).newPage(),
      errors = [];
    for (const page of [owner, manager])
      page.on('pageerror', (error) => errors.push(error.message));
    const base = process.env.MOA_TEST_URL;
    async function login(page, name, password) {
      await page.goto(base + '/login');
      await page.locator('#login-username').fill(name);
      await page.locator('#login-password').fill(password);
      await page.locator('#login-button').click();
      await page.waitForURL(base + '/');
    }
    await login(owner, 'owner', 'fixture-owner-password');
    await owner.locator('a[href="#sharing"]:visible').first().click();
    await owner.waitForFunction(() =>
      document.querySelector('#members-state').textContent.includes('owner'),
    );
    await owner.locator('#member-name').fill('partner');
    await owner.locator('#member-password').fill('fixture-partner-password');
    await owner.locator('#member-confirm').fill('fixture-partner-password');
    await owner.locator('#member-form button').click();
    await owner.waitForSelector('[data-member-id]');
    assert.equal(await owner.locator('#member-password').inputValue(), '');
    await login(manager, 'partner', 'fixture-partner-password');
    await manager.locator('a[href="#sharing"]:visible').first().click();
    await manager.waitForFunction(() =>
      document.querySelector('#members-state').textContent.includes('partner'),
    );
    assert.equal(await manager.locator('#members-owner').isHidden(), true);
    assert.equal(await manager.locator('#account-role').textContent(), '● 공동관리자');
    for (const page of [owner, manager]) {
      await page.goto(base + '/');
      await page.locator('#dashboard-bill-open').click();
      await page.waitForSelector('.bill-form');
      await page.locator('#finance-month').fill('2026-10');
      await page.locator('#finance-month').dispatchEvent('change');
      await page.waitForFunction(
        () => document.querySelector('.bill-form')?.dataset.month === '2026-10',
      );
      const form = page.locator('.bill-form[data-card="삼성"]');
      await form.locator('[name=statement_status]').selectOption('received');
      await form.locator('[name=amount]').fill(page === owner ? '100' : '200');
    }
    await owner.locator('.bill-form[data-card="삼성"] button').click();
    await owner.waitForFunction(() =>
      document.querySelector('#billing-stats').textContent.includes('100원'),
    );
    await manager.locator('.bill-form[data-card="삼성"] button').click();
    await manager.waitForFunction(() =>
      document.querySelector('#billing-stats').textContent.includes('200원'),
    );
    assert.equal(await manager.locator('#conflict-notice').isVisible(), false);
    const ownBill = await owner.evaluate(() =>
      fetch('/api/billing?month=2026-10').then((r) => r.json()),
    );
    assert.equal(ownBill.total, 100);

    // 같은 계정의 두 화면은 여전히 충돌을 확인하고, 다른 사람의 저장과는 충돌하지 않습니다.
    const ownerTab = await owner.context().newPage();
    ownerTab.on('pageerror', (error) => errors.push(error.message));
    await ownerTab.goto(base + '/');
    await ownerTab.locator('#dashboard-bill-open').click();
    await ownerTab.locator('#finance-month').fill('2026-10');
    await ownerTab.locator('#finance-month').dispatchEvent('change');
    await ownerTab.waitForFunction(
      () => document.querySelector('.bill-form')?.dataset.month === '2026-10',
    );
    await ownerTab.locator('.bill-form[data-card="삼성"] [name=amount]').fill('250');
    await owner.locator('.bill-form[data-card="삼성"] [name=amount]').fill('150');
    await owner.locator('.bill-form[data-card="삼성"] button').click();
    await owner.waitForFunction(() =>
      document.querySelector('#billing-stats').textContent.includes('150원'),
    );
    await ownerTab.locator('.bill-form[data-card="삼성"] button').click();
    await ownerTab.waitForSelector('#conflict-notice:visible');
    assert.equal(
      await ownerTab.locator('.bill-form[data-card="삼성"] [name=amount]').inputValue(),
      '250',
    );
    await ownerTab.close();

    // 개인 파일은 각자 저장하고, 통합 화면에는 이름 없는 합계만 나타납니다.
    async function upload(page, merchant, amount) {
      const result = await page.evaluate(
        async ({ merchant, amount }) => {
          const form = new FormData();
          form.append(
            'file',
            new File([`이용일,가맹점,금액\n2026-11-01,${merchant},${amount}`], merchant + '.csv'),
          );
          form.append('header', '1');
          const preview = await fetch('/api/preview', {
            method: 'POST',
            headers: { 'X-Requested-With': 'CardDashboard' },
            body: form,
          }).then((r) => r.json());
          const snapshot = await fetch('/api/data');
          const saved = await fetch('/api/import', {
            method: 'POST',
            headers: {
              'X-Requested-With': 'CardDashboard',
              'Content-Type': 'application/json',
              'If-Match': snapshot.headers.get('X-Moa-Revision'),
            },
            body: JSON.stringify({
              token: preview.token,
              card: '삼성',
              date: 0,
              merchant: 1,
              amount: 2,
            }),
          });
          return saved.status;
        },
        { merchant, amount },
      );
      assert.equal(result, 200);
    }
    await upload(owner, '스타벅스_가상개인A', 1000);
    await upload(manager, '가상의원_개인B', 2000);
    for (const [page, own, other] of [
      [owner, '가상개인A', '개인B'],
      [manager, '개인B', '가상개인A'],
    ]) {
      await page.goto(base + '/');
      await page.waitForFunction(
        (own) => document.querySelector('#transactions').textContent.includes(own),
        own,
      );
      assert.equal((await page.locator('#transactions').textContent()).includes(other), false);
      await page.locator('[data-dashboard-scope="household"]').click();
      await page.locator('#month').fill('2026-11');
      await page.locator('#month').dispatchEvent('change');
      await page.waitForFunction(() =>
        document.querySelector('#household-stats').textContent.includes('3,000원'),
      );
      const text = await page.locator('#household-dashboard').textContent();
      assert.equal(text.includes(own), false);
      assert.equal(text.includes(other), false);
      assert(text.includes('커피'));
      assert(text.includes('의료'));
      assert(text.includes('삼성'));
    }
    const planStatus = await manager.evaluate(async () => {
      const snapshot = await fetch('/api/finance?month=2026-10');
      return (
        await fetch('/api/installments', {
          method: 'POST',
          headers: {
            'X-Requested-With': 'CardDashboard',
            'Content-Type': 'application/json',
            'If-Match': snapshot.headers.get('X-Moa-Revision'),
          },
          body: JSON.stringify({
            name: '가상함께보는할부',
            card: '삼성',
            principal: 900,
            months: 3,
            first_month: '2026-10',
            paid_count: 0,
          }),
        })
      ).status;
    });
    assert.equal(planStatus, 200);
    for (const page of [owner, manager]) {
      await page.locator('.mobile-nav a[href="#planning"]').click();
      await page.locator('[data-management-scope="shared"]').click();
      await page.locator('#finance-month').fill('2026-10');
      await page.locator('#finance-month').dispatchEvent('change');
      await page.waitForFunction(() =>
        document.querySelector('#shared-management-state').textContent.includes('2026-10 ·'),
      );
      assert.equal(await page.locator('.shared-management-member').count(), 2);
      const sharedText = await page.locator('#shared-management-list').textContent();
      assert(sharedText.includes('owner'));
      assert(sharedText.includes('partner'));
      assert(sharedText.includes('150원'));
      assert(sharedText.includes('200원'));
      assert(sharedText.includes('가상함께보는할부'));
      assert.equal(
        await page
          .locator(
            '#shared-management-list input,#shared-management-list select,#shared-management-list textarea,#shared-management-list button',
          )
          .count(),
        0,
      );
      for (const width of [320, 390, 768, 1280]) {
        await page.setViewportSize({ width, height: 844 });
        assert.equal(
          await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
          false,
        );
      }
      await page.setViewportSize({ width: 390, height: 844 });
    }
    // 대시보드의 내 카드값 바로가기는 공유 열람 모드에서 돌아와도 개인 입력 화면을 엽니다.
    await owner.locator('.mobile-nav a[href="#dashboard"]').click();
    await owner.locator('[data-dashboard-scope="personal"]').click();
    await owner.locator('#dashboard-bill-open').click();
    assert.equal(await owner.locator('#personal-management').isVisible(), true);
    assert.equal(await owner.locator('#shared-management').isHidden(), true);
    // 개인 카드사 추가·표시 이름과 할부 선택지는 다른 사람의 원장에 영향을 주지 않습니다.
    await owner.waitForSelector('.bill-setting', { state: 'attached' });
    await owner.locator('#billing-card-add').evaluate((form) => (form.parentElement.open = true));
    await owner.locator('#billing-card-add [name=card]').fill('가상추가');
    await owner.locator('#billing-card-add [name=display_name]').fill('개인표시<script>');
    await owner.locator('#billing-card-add [name=due_day]').fill('25');
    await owner.locator('#billing-card-add button').click();
    await owner.waitForSelector('.bill-form[data-card="가상추가"]');
    assert.match(
      await owner.locator('.bill-form[data-card="가상추가"] h3').textContent(),
      /개인표시<script>/,
    );
    assert.equal(await owner.locator('#billing-list script').count(), 0);
    await owner.waitForFunction(() =>
      [...document.querySelector('#plan-card').options].some(
        (option) => option.value === '가상추가',
      ),
    );
    const partnerCards = await manager.evaluate(() =>
      fetch('/api/billing?month=2026-11').then((response) => response.json()),
    );
    assert.equal(
      partnerCards.cards.some((row) => row.card === '가상추가'),
      false,
    );
    await owner.locator('[data-finance-tab="installments"]').click();
    await owner.locator('#installment-editor').evaluate((element) => (element.open = true));
    await owner.locator('#plan-name').fill('가상개인할부');
    await owner.locator('#plan-card').selectOption('가상추가');
    await owner.locator('#plan-principal').fill('900');
    await owner.locator('#plan-months').fill('3');
    await owner.locator('#plan-first-month').fill('2026-11');
    await owner.locator('#plan-save').click();
    await owner.waitForFunction(() =>
      document.querySelector('#installment-list').textContent.includes('가상개인할부'),
    );
    assert.match(await owner.locator('#installment-list').textContent(), /개인표시<script>/);
    await owner.locator('[data-finance-tab="billing"]').click();
    await owner.evaluate(() => loadBilling());
    const customSetting = owner.locator('.bill-setting[data-card="가상추가"]');
    await customSetting.locator('[name=enabled]').uncheck();
    await customSetting.locator('button').click();
    await owner.waitForFunction(() => !document.querySelector('.bill-form[data-card="가상추가"]'));
    await owner.locator('[data-finance-tab="installments"]').click();
    await owner
      .locator('#installment-list .finance-item')
      .filter({ hasText: '가상개인할부' })
      .locator('[data-plan-edit]')
      .click();
    assert.equal(await owner.locator('#plan-card').inputValue(), '가상추가');
    assert.match(await owner.locator('#plan-card option:checked').textContent(), /사용 해제/);
    for (const width of [320, 390, 768, 1280]) {
      await owner.setViewportSize({ width, height: 844 });
      assert.equal(
        await owner.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
    }
    await owner.setViewportSize({ width: 390, height: 844 });
    await owner.locator('[data-management-scope="shared"]').click();
    await owner.waitForFunction(() =>
      document.querySelector('#shared-management-list').textContent.includes('개인표시<script>'),
    );
    assert.equal(await owner.locator('#shared-management-list script').count(), 0);
    await owner.locator('a[href="#sharing"]:visible').first().click();
    await owner.waitForFunction(() =>
      document.querySelector('#audit-list').textContent.includes('owner'),
    );
    assert.equal((await owner.locator('#audit-list').textContent()).includes('partner'), false);
    for (const width of [320, 390, 768, 1280]) {
      await owner.setViewportSize({ width, height: 844 });
      assert.equal(
        await owner.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
    }
    await owner.setViewportSize({ width: 390, height: 844 });
    await owner.locator('[data-member-active="false"]').click();
    await owner.waitForSelector('[data-member-active="true"]');
    await manager.reload();
    await manager.waitForURL((url) => url.pathname === '/login');
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
