/**
 * 실제 서비스 워커를 설치해 오프라인 캐시에 금융 응답이 남지 않는지 확인합니다.
 * 홈 화면 설치 UI는 OS 대신 브라우저 이벤트로 검증하며 실제 기기 설치를 대신하지 않습니다.
 */
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
    await page.evaluate(async () => {
      await navigator.serviceWorker.ready;
      if (!navigator.serviceWorker.controller) {
        await new Promise((resolve) =>
          navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true }),
        );
      }
    });
    // Chrome이 실제 문서의 설치 명세를 오류 없이 해석하는지도 확인합니다.
    const devtools = await context.newCDPSession(page);
    const manifest = await devtools.send('Page.getAppManifest');
    assert.equal(manifest.url, base + '/static/manifest.webmanifest');
    assert.deepEqual(manifest.errors, []);
    await page.locator('#pwa-install-guide summary').click();
    assert((await page.locator('#pwa-install-status').textContent()).includes('Chrome'));

    // 브라우저가 설치 가능 이벤트를 보내도 사용자 클릭 전에는 설치 창을 열지 않습니다.
    await page.evaluate(() => {
      window.fixturePromptCalls = 0;
      const event = new Event('beforeinstallprompt', { cancelable: true });
      Object.defineProperties(event, {
        prompt: {
          value: async () => {
            window.fixturePromptCalls++;
          },
        },
        userChoice: { value: Promise.resolve({ outcome: 'dismissed' }) },
      });
      window.dispatchEvent(event);
    });
    assert.equal(await page.evaluate(() => window.fixturePromptCalls), 0);
    await page.locator('#pwa-install').click();
    assert.equal(await page.evaluate(() => window.fixturePromptCalls), 1);
    await page.waitForFunction(() => document.getElementById('pwa-install').hidden);
    await page.evaluate(() => window.dispatchEvent(new Event('appinstalled')));
    assert((await page.locator('#pwa-install-status').textContent()).includes('설치했습니다.'));

    await page.locator('#login-password').fill('fixture-owner-password');
    await page.locator('#login-button').click();
    await page.waitForURL(base + '/');
    await page.waitForFunction(() =>
      document.getElementById('transactions').textContent.includes('가상PWA전용상점'),
    );
    await page.evaluate(() => fetch('/api/data').then((response) => response.json()));

    // 가상 거래를 읽은 뒤에도 금융 API·로그인 HTML이 캐시에 남지 않는지 확인합니다.
    const stored = await page.evaluate(async () => {
      const entries = [];
      for (const name of await caches.keys()) {
        const cache = await caches.open(name);
        for (const request of await cache.keys()) {
          entries.push({ name, url: request.url, text: await (await cache.match(request)).text() });
        }
      }
      return {
        entries,
        local: localStorage.length,
        session: sessionStorage.length,
        databases: await indexedDB.databases(),
      };
    });
    assert.equal(stored.entries.length, 1);
    assert.equal(stored.entries[0].url, base + '/static/offline.html');
    assert.equal(stored.entries[0].text.includes('가상PWA전용상점'), false);
    assert.equal(stored.entries[0].text.includes('fixture-owner-password'), false);
    assert.equal(stored.local, 0);
    assert.equal(stored.session, 0);
    assert.deepEqual(stored.databases, []);

    await context.setOffline(true);
    await page.waitForSelector('#pwa-network-status:visible');
    await page.goto(base + '/');
    assert.equal(await page.locator('h1').textContent(), '모아에 연결할 수 없어요');
    assert.equal((await page.locator('body').textContent()).includes('가상PWA전용상점'), false);
    const apiFailed = await page.evaluate(() =>
      fetch('/api/data')
        .then(() => false)
        .catch(() => true),
    );
    assert.equal(apiFailed, true);
    await context.setOffline(false);
    await page.getByRole('link', { name: '다시 연결하기' }).click();
    await page.waitForSelector('#app-main');
    await page.waitForFunction(() =>
      document.getElementById('transactions').textContent.includes('가상PWA전용상점'),
    );

    // 같은 브라우저의 다른 탭에서 계정을 바꾸면 이전 사람의 화면을 새 계정으로 교체합니다.
    await page.evaluate(async () => {
      const snapshot = await fetch('/api/members');
      await fetch('/api/members', {
        method: 'POST',
        headers: {
          'X-Requested-With': 'CardDashboard',
          'Content-Type': 'application/json',
          'If-Match': snapshot.headers.get('X-Moa-Revision'),
        },
        body: JSON.stringify({ username: 'pwa_partner', password: 'fixture-partner-password' }),
      });
    });
    const partnerPage = await context.newPage();
    partnerPage.on('pageerror', (error) => errors.push(error.message));
    await partnerPage.goto(base + '/');
    await partnerPage.waitForSelector('#app-main');
    // 다른 탭의 계정 전환을 재현하기 위해 인증 API로 새 세션을 발급합니다.
    await partnerPage.evaluate(async () => {
      await fetch('/api/login', {
        method: 'POST',
        headers: { 'X-Requested-With': 'CardDashboard', 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'pwa_partner', password: 'fixture-partner-password' }),
      });
    });
    // 화면 복귀를 명시적으로 발생시킵니다. 헤드리스 브라우저의 창 활성화만으로는
    // 실제 휴대폰의 pageshow/visibility 이벤트가 항상 발생하지 않습니다.
    await page.evaluate(() =>
      window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })),
    );
    await page.waitForFunction(() => document.body.dataset.userId === '2');
    await page.bringToFront();
    await page.waitForSelector('#app-main');
    assert.equal(
      (await page.locator('#transactions').textContent()).includes('가상PWA전용상점'),
      false,
    );
    await partnerPage.close();

    // 로그아웃된 세션의 이전 화면으로 복귀해도 다시 로그인하도록 확인합니다.
    await page.evaluate(async () => {
      await fetch('/api/logout', {
        method: 'POST',
        headers: { 'X-Requested-With': 'CardDashboard' },
      });
      window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    });
    await page.waitForURL((url) => url.pathname === '/login');
    await context.setOffline(true);
    await page.goto(base + '/');
    assert.equal(await page.locator('h1').textContent(), '모아에 연결할 수 없어요');
    await context.setOffline(false);

    const apple = await browser.newContext({
      viewport: { width: 390, height: 844 },
      userAgent:
        'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1',
      isMobile: true,
      hasTouch: true,
    });
    const applePage = await apple.newPage();
    applePage.on('pageerror', (error) => errors.push(error.message));
    await applePage.goto(base + '/login');
    await applePage.locator('#pwa-install-guide summary').click();
    assert((await applePage.locator('#pwa-install-status').textContent()).includes('Safari'));
    assert.equal(await applePage.locator('#pwa-install').isVisible(), false);
    for (const width of [320, 390, 768, 1280]) {
      await applePage.setViewportSize({ width, height: 844 });
      assert.equal(
        await applePage.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
