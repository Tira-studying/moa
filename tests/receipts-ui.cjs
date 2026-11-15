/** 가상 거래·실제 임시 서버로 영수증 사진 올리기·크게 보기·지우기, 새 직접 입력의 사진, 작은 화면을 검증합니다. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

/** 페이지 안 canvas로 가로로 긴 가짜 사진(PNG)을 만들어 파일 선택 칸에 넣을 수 있게 돌려줍니다. */
async function fakePhoto(page, width, height, name) {
  const base64 = await page.evaluate(
    async ([w, h]) => {
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      const context = canvas.getContext('2d');
      context.fillStyle = '#f4efe2';
      context.fillRect(0, 0, w, h);
      context.fillStyle = '#333';
      context.fillRect(40, 40, w / 2, 60);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
      const bytes = new Uint8Array(await blob.arrayBuffer());
      let text = '';
      for (const byte of bytes) text += String.fromCharCode(byte);
      return btoa(text);
    },
    [width, height],
  );
  return { name, mimeType: 'image/png', buffer: Buffer.from(base64, 'base64') };
}

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
    await page.goto(base + '/#activity');
    await page.waitForFunction(() =>
      document.querySelector('#transactions').textContent.includes('가상 마트'),
    );

    // 1. 거래 상세에서 사진을 고르면 바로 저장되고, 긴 변 1600px JPEG로 줄어듭니다.
    const editor = page.locator('#transaction-editor');
    await page.locator('[data-transaction-details="101"]').click();
    await editor
      .locator('[data-receipt-input]')
      .setInputFiles(await fakePhoto(page, 3200, 1600, 'receipt.png'));
    await editor.locator('.receipt-thumb img[src^="data:image/jpeg"]').waitFor();
    assert.match(await editor.locator('[data-receipt-state]').textContent(), /저장했어요/);
    const stored = await page.evaluate(async () => {
      const [row] = (await api('/api/data')).transactions;
      const response = await fetch(`/api/receipts/${row.receipt_ids}`);
      const bitmap = await createImageBitmap(await response.blob());
      return [response.headers.get('content-type'), bitmap.width, bitmap.height];
    });
    assert.deepEqual(stored, ['image/jpeg', 1600, 800]);

    // 2. 크게 보기 → 닫기. 사진을 올린 뒤에도 메모 저장이 버전 충돌 없이 됩니다.
    await editor.locator('.receipt-open').click();
    assert.equal(await page.locator('#receipt-viewer').isVisible(), true);
    await page.locator('#receipt-viewer-close').click();
    assert.equal(await editor.isVisible(), true);
    await page.locator('#transaction-memo').fill('가상 장보기');
    await page.locator('#transaction-edit-save').click();
    await page.waitForFunction(() => !document.querySelector('#transaction-editor').open);
    assert.equal(await page.locator('#conflict-notice').isHidden(), true);
    await page.waitForFunction(() =>
      document.querySelector('#transactions').textContent.includes('영수증 사진 1장'),
    );

    // 3. 저장 없이 닫아도 목록이 갱신되고, 지우면 표시가 사라집니다.
    await page.locator('[data-transaction-details="101"]').click();
    assert.equal(await editor.locator('.receipt-thumb').count(), 1);
    await editor
      .locator('[data-receipt-input]')
      .setInputFiles(await fakePhoto(page, 600, 900, 'second.png'));
    await page.waitForFunction(
      () => document.querySelectorAll('#transaction-editor .receipt-thumb').length === 2,
    );
    await page.locator('#transaction-edit-close').click();
    await page.waitForFunction(() =>
      document.querySelector('#transactions').textContent.includes('영수증 사진 2장'),
    );
    await page.locator('[data-transaction-details="101"]').click();
    await editor.locator('.receipt-remove').first().click();
    await page.waitForFunction(
      () => document.querySelectorAll('#transaction-editor .receipt-thumb').length === 1,
    );
    await editor.locator('.receipt-remove').first().click();
    await page.waitForFunction(() => !document.querySelector('#transaction-editor .receipt-thumb'));
    await page.locator('#transaction-edit-close').click();
    await page.waitForFunction(
      () => !document.querySelector('#transactions').textContent.includes('영수증 사진'),
    );

    // 4. 새 직접 입력: 저장 전에 고른 사진은 저장할 때 함께 올라갑니다.
    await page.locator('#activity [data-manual-open]').click();
    await page.locator('#manual-merchant').fill('가상 시장');
    await page.locator('#manual-amount').fill('8000');
    await page
      .locator('#manual-receipts [data-receipt-input]')
      .setInputFiles(await fakePhoto(page, 800, 1200, 'market.png'));
    await page.locator('#manual-receipts .receipt-thumb').waitFor();
    assert.match(
      await page.locator('#manual-receipts [data-receipt-state]').textContent(),
      /저장하면/,
    );
    await page.locator('#manual-entry-save').click();
    await page.waitForFunction(() => !document.querySelector('#manual-entry').open);
    await page.waitForFunction(() =>
      /가상 시장[\s\S]*영수증 사진 1장/.test(document.querySelector('#transactions').textContent),
    );
    await page.locator('[data-manual-edit]').click();
    await page.locator('#manual-receipts .receipt-thumb img[src^="data:image/jpeg"]').waitFor();
    await page.locator('#manual-entry-close').click();

    // 5. 작은 화면: 사진 칸이 있는 창이 가로로 넘치지 않습니다.
    await page.setViewportSize({ width: 375, height: 812 });
    await page.locator('[data-transaction-details="101"]').click();
    const box = await editor.boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 375, JSON.stringify(box));
    assert.ok(
      await page.evaluate(() => {
        const form = document.querySelector('#transaction-detail-form');
        return form.scrollWidth <= form.clientWidth + 1;
      }),
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
