/**
 * 가상 자료로 브라우저 화면 또는 계산 결과를 검증합니다.
 * 실제 거래·암호는 사용하지 않습니다. 브라우저 테스트는 Chromium 샌드박스를 켜고 실행합니다.
 */

const fs = require('fs'),
  path = require('path');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
  });
  try {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      isMobile: true,
      hasTouch: true,
    });
    let previewCard = '삼성';
    const page = await context.newPage(),
      errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === '/')
        return route.fulfill({
          contentType: 'text/html; charset=utf-8',
          body: fs.readFileSync('templates/index.html', 'utf8'),
        });
      if (url.pathname.startsWith('/static/'))
        return route.fulfill({
          contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css',
          body: fs.readFileSync(path.join('.', url.pathname)),
        });
      if (url.pathname === '/api/data')
        return route.fulfill({ json: { transactions: [], imports: [] } });
      if (url.pathname === '/api/mail')
        return route.fulfill({
          json: {
            configured: true,
            folder: 'MOA',
            auto_password: true,
            items: [
              {
                id: 'fixture',
                filename: 'WooriCard.html',
                card: '우리',
                size: 100,
                stage: 'received',
                state: 'waiting',
                created: '2026-10-30 00:00:00',
              },
            ],
            summary: { received: 1 },
            total: 1,
          },
        });
      if (url.pathname.endsWith('/prepare')) {
        previewCard = route.request().postDataJSON().card;
        if (previewCard !== '우리') throw Error('Woori mail card identity lost');
      }
      if (url.pathname.endsWith('/prepare') || url.pathname === '/api/secure-html')
        return route.fulfill({
          json: {
            token: 'fixture',
            tables: [
              {
                index: 0,
                count: 2,
                suggested_header: 1,
                rows: [
                  ['이용일', '가맹점', '금액'],
                  ['2026-10-01', 'fixture', '1000'],
                ],
              },
            ],
          },
        });
      if (url.pathname === '/api/secure-preview' || url.pathname === '/api/preview')
        return route.fulfill({
          json: {
            token: 'fixture',
            card: previewCard,
            count: 1,
            columns: ['이용일', '가맹점', '금액'],
            rows: [['2026-10-01', 'fixture', '1000']],
          },
        });
      if (url.pathname === '/api/import') return route.fulfill({ json: { count: 1 } });
      return route.abort();
    });
    const visible = (id) => page.locator('#' + id).isVisible();
    const nav = async (id) => {
      const direct = page.locator(`.mobile-nav a[href="#${id}"]`);
      if (await direct.count()) return direct.click();
      await page.locator('.mobile-nav a[href="#sharing"]').click();
      await page.locator(`#sharing .account-shortcuts a[href="#${id}"]`).click();
    };
    const check = async () => {
      if ((await page.locator('[data-view]:visible').count()) !== 1)
        throw Error('Multiple pages visible');
      if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth))
        throw Error('Horizontal overflow');
    };
    await page.goto('https://fixture.invalid');
    await check();
    if (process.env.MOA_UI_SCREENSHOT)
      await page.screenshot({ path: path.join(process.env.MOA_UI_SCREENSHOT, 'dashboard.png') });
    if (!(await visible('dashboard'))) throw Error('Initial dashboard missing');
    await nav('upload');
    await check();
    if (!(await visible('upload-form')) || (await visible('secure-form')))
      throw Error('Upload forms must be separated');
    await page.locator('#upload-card').selectOption('삼성');
    await page
      .locator('#file')
      .setInputFiles({ name: 'fixture.csv', mimeType: 'text/csv', buffer: Buffer.from('fixture') });
    await page.locator('#preview-button').click();
    await page.locator('#mapping').waitFor({ state: 'visible' });
    await check();
    await page.locator('#review-back').click();
    if (!(await visible('upload'))) throw Error('Spreadsheet review back failed');
    await nav('history');
    await check();
    await nav('upload');
    if ((await page.locator('#upload-card').inputValue()) !== '삼성')
      throw Error('Form state lost during navigation');
    await page.locator('[data-upload-mode="secure"]').click();
    if ((await visible('upload-form')) || !(await visible('secure-form')))
      throw Error('Secure upload tab failed');
    await page.locator('#secure-file').setInputFiles({
      name: 'fixture.html',
      mimeType: 'text/html',
      buffer: Buffer.from('fixture'),
    });
    await page.locator('#secure-password').fill('fixture-only');
    await page.locator('#unlock-button').click();
    await page.locator('#secure-selection').waitFor({ state: 'visible' });
    await check();
    if (!(await visible('review')) || (await visible('upload')))
      throw Error('Secure result must use review page');
    await page.locator('#secure-use').click();
    await page.locator('#mapping').waitFor({ state: 'visible' });
    if (await visible('secure-selection')) throw Error('Previous step still visible');
    await page.locator('#review-back').click();
    if (!(await visible('secure-selection')) || (await visible('mapping')))
      throw Error('Back to table selection failed');
    await nav('mail-inbox');
    if (process.env.MOA_UI_SCREENSHOT)
      await page.screenshot({ path: path.join(process.env.MOA_UI_SCREENSHOT, 'mail.png') });
    await page.locator('[data-mail-open]').click();
    await page.locator('#secure-selection').waitFor({ state: 'visible' });
    if (
      (await page.locator('#secure-card').inputValue()) !== '우리' ||
      (await page.locator('#secure-online').isChecked())
    )
      throw Error('Woori HTML selection failed');
    await page.locator('#secure-use').click();
    await page.locator('#mapping').waitFor({ state: 'visible' });
    await page.locator('#import-button').click();
    await page.locator('#mail-inbox').waitFor({ state: 'visible' });
    await check();
    await page.goBack();
    await check();
    await page.goto('https://fixture.invalid/#history');
    await check();
    if (!(await visible('history'))) throw Error('Direct links must work');

    await page.setViewportSize({ width: 1280, height: 900 });
    await page.locator('aside .nav[href="#dashboard"]').click();
    await check();
    if (!(await visible('dashboard')) || (await page.locator('.mobile-nav').isVisible()))
      throw Error('Desktop navigation failed');
    if (errors.length) throw Error(errors.join('\n'));
    await context.close();
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
