/**
 * 설치 확인용 실행 파일: Chromium이 샌드박스를 켠 상태로 시작되는지만 확인합니다.
 */

const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.MOA_CHROMIUM || '/usr/bin/chromium',
    chromiumSandbox: true,
    headless: true,
  });
  await browser.close();
  console.log('Chromium sandbox: OK');
})().catch(() => {
  console.error(
    'Chromium sandbox unavailable. Do not use --no-sandbox or privileged mode. Check the host kernel/container sandbox support.',
  );
  process.exit(1);
});
