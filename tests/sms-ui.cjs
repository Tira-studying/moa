/** 가상 문자와 실제 서버로 단축어 수신·개인 화면·키 노출·모바일 배치를 검증합니다. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await context.newPage();
    const errors = [];
    let smsReads = 0;
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
      if (new URL(request.url()).pathname === '/api/sms') smsReads++;
    });
    const base = process.env.MOA_TEST_URL;
    await page.goto(base + '/login');
    await page.locator('#login-password').fill('fixture-owner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');
    await page.locator('#personal-dashboard').waitFor({ state: 'visible' });
    for (const width of [390, 1280]) {
      await page.setViewportSize({ width, height: 844 });
      assert.equal(await page.locator('#sms-dashboard').isHidden(), true);
      assert.equal(await page.locator('aside a[href="#sms"]').isHidden(), true);
    }
    await page.evaluate(() => loadSmsDashboard());
    assert.equal(smsReads, 0);
    await page.setViewportSize({ width: 390, height: 844 });
    // 메뉴를 숨겨도 기존 주소로 직접 열면 수신·조회·대조 기능을 계속 쓸 수 있습니다.
    await page.goto(base + '/#sms');
    await page.waitForFunction(() =>
      document.querySelector('#sms-key-state').textContent.includes('발급'),
    );
    await page.locator('#sms-setup summary').click();
    await page.locator('#sms-key-create').click();
    await page.waitForFunction(() => document.querySelector('#sms-key-value').value.length === 43);
    const key = await page.locator('#sms-key-value').inputValue();
    assert.equal(await page.locator('#sms-endpoint').inputValue(), base + '/api/sms/receive');
    const now = new Date(Date.now() + 9 * 3600000 - 120000).toISOString();
    const stamp = now.slice(5, 16).replace('-', '/').replace('T', ' ');
    await page.locator('#sms-month').fill(now.slice(0, 7));
    await page.locator('#sms-month').dispatchEvent('change');
    async function send(kind, token = key) {
      return context.request.post(base + '/api/sms/receive', {
        headers: { 'X-Requested-With': 'CardDashboard', Authorization: 'Bearer ' + token },
        data: {
          text: `[Web발신]\nKB국민카드2222${kind}\n가*상님\n12,400원 일시불\n${stamp}\n가상<script>상점\n누적80,000원`.replaceAll(
            '\n',
            ' ',
          ),
        },
      });
    }
    assert.equal((await send('승인')).status(), 201);
    assert.equal((await send('취소')).status(), 201);
    assert.equal((await send('승인')).status(), 200);
    await page.locator('#sms-refresh').click();
    await page.waitForSelector('#sms-list .finance-item');
    assert.equal(await page.locator('#sms-list .finance-item').count(), 2);
    assert.equal(await page.locator('#sms-list script').count(), 0);
    assert.match(await page.locator('#sms-stats').textContent(), /문자 기준 순사용액0원/);
    assert.match(await page.locator('#sms-list').textContent(), /가상<script>상점/);
    assert.match(await page.locator('#sms-key-state').textContent(), /UTC/);
    const ledger = await context.request.get(base + '/api/data');
    assert.deepEqual((await ledger.json()).transactions, []);
    await page
      .locator('#sms-list .finance-item')
      .last()
      .locator('[data-sms-status="settled"]')
      .click();
    await page.waitForFunction(() =>
      document.querySelector('#sms-state').textContent.includes('미대조 1건'),
    );
    // 대조 완료는 소비나 기본 목록을 숨기지 않습니다.
    assert.equal(await page.locator('#sms-list .finance-item').count(), 2);
    assert.equal(await page.locator('#sms-show-completed').isChecked(), false);
    assert.match(await page.locator('#sms-stats').textContent(), /문자 기준 순사용액0원/);
    const settledRow = page
      .locator('#sms-list .finance-item')
      .filter({ has: page.locator('[data-sms-status="pending"]') });
    await settledRow.locator('[data-sms-status="ignored"]').click();
    await page.waitForFunction(
      () => document.querySelectorAll('#sms-list .finance-item').length === 1,
    );
    assert.match(await page.locator('#sms-stats').textContent(), /-12,400원/);
    await page.locator('#sms-show-completed').check();
    await page.waitForFunction(
      () => document.querySelectorAll('#sms-list .finance-item').length === 2,
    );
    await page.locator('[data-sms-status="pending"]').click();
    await page.waitForFunction(() =>
      document.querySelector('#sms-state').textContent.includes('미대조 2건'),
    );
    await page.locator('#sms-show-completed').uncheck();
    assert.match(await page.locator('#sms-stats').textContent(), /문자 기준 순사용액0원/);
    // 같은 수신 키로 카드사별 양식을 받아 화면에서 구분합니다. 원문은 모두 가상입니다.
    const extraMessages = [
      `[Web발신]\n신한카드(3333)승인 가*상 12,400원(일시불)${stamp} 가상상점 누적80,000원`,
      `[Web발신]\n삼성4444취소 가*상\n-12,400원 일시불\n${stamp} 가상상점\n누적80,000원`,
      `[Web발신]\n현대 가상<script>상품 승인\n가*상\n12,400원 일시불\n${stamp}\n가상상점\n누적80,000원`,
      `[Web발신]\n현대 가상<script>상품 취소\n가*상\n12,400원 일시불\n${stamp}\n가상상점\n누적80,000원`,
    ];
    for (const text of extraMessages) {
      const response = await context.request.post(base + '/api/sms/receive', {
        headers: { 'X-Requested-With': 'CardDashboard', Authorization: 'Bearer ' + key },
        data: { text },
      });
      assert.equal(response.status(), 201);
    }
    await page.locator('#sms-refresh').click();
    await page.waitForFunction(
      () => document.querySelectorAll('#sms-list .finance-item').length === 6,
    );
    assert.match(await page.locator('#sms-list').textContent(), /신한/);
    assert.match(await page.locator('#sms-list').textContent(), /삼성/);
    assert.match(
      await page.locator('#sms-list').textContent(),
      /현대 · 번호 미표시 · 상품 가상<script>상품/,
    );
    assert.equal(await page.locator('#sms-list script').count(), 0);
    assert.match(await page.locator('#sms-stats').textContent(), /문자 기준 순사용액0원/);
    for (const width of [320, 390, 768, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => !document.querySelector('#sms-refresh').disabled);
    const readsBeforeDashboard = smsReads;
    await page.locator('.mobile-nav a[href="#dashboard"]').click();
    await page.evaluate(() => loadSmsDashboard());
    assert.equal(await page.locator('#sms-dashboard').isHidden(), true);
    assert.equal(smsReads, readsBeforeDashboard);
    assert.equal(await page.locator('#sms-key-value').inputValue(), '');
    assert.equal(await page.locator('#sms-key-result').isHidden(), true);
    await page.goto(base + '/#sms');
    await page.waitForFunction(() => !document.querySelector('#sms-refresh').disabled);
    await page.locator('#sms-key-create').click();
    await page.waitForFunction(() => document.querySelector('#sms-key-value').value.length === 43);
    assert.notEqual(await page.locator('#sms-key-value').inputValue(), key);
    assert.equal((await send('승인')).status(), 401);
    await page.locator('#sms-key-revoke').click();
    await page.waitForFunction(() =>
      document.querySelector('#sms-key-state').textContent.includes('발급'),
    );
    assert.equal(await page.locator('#sms-key-value').inputValue(), '');
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
