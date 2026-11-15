/**
 * 보안 HTML 판독의 실행 파일입니다. Python이 표준 입력으로 보낸 원본·암호를 받아
 * 격리된 Chromium에서 정상 폼을 제출하고 표·목록을 읽습니다. 화면에 일부 거래만 있으면
 * 더보기를 처리하고 완전성을 확인합니다. 로그에 원본·암호·예외 본문을 출력하지 않습니다.
 */

// Receives credentials only via stdin. Never logs document content or exceptions.
const { chromium } = require('playwright');
const { extractTables } = require('./table-reader.cjs');
const { extractList } = require('./list-reader.cjs');
const { extractLotte } = require('./lotte-reader.cjs');
const { readWritten } = require('./written-reader.cjs');
const { allowedUrl, fetchSamsung } = require('./samsung-network.cjs');
const { prepareStatementControls } = require('./statement-controls.cjs');
const { expandStatement } = require('./expand-statement.cjs');
let browser;
const emit = (obj) => process.stdout.write(JSON.stringify(obj));
(async () => {
  let input = '';
  for await (const chunk of process.stdin) {
    input += chunk;
    if (input.length > 12 * 1024 * 1024) throw new Error('size');
  }
  const payload = JSON.parse(input);
  input = '';
  const timer = setTimeout(
    () => process.exit(2),
    payload.allowOnline && payload.card === '삼성' ? 50000 : 25000,
  );
  try {
    browser = await chromium.launch({
      ...(process.env.MOA_CHROMIUM ? { executablePath: process.env.MOA_CHROMIUM } : {}),
      chromiumSandbox: true,
      headless: true,
      args: ['--disable-background-networking', '--disable-breakpad', '--disable-crash-reporter'],
    });
    const card = payload.card || '삼성';
    const allowOnline = payload.allowOnline === true && card === '삼성';
    const budget = { bytes: 0 };
    let onlineRequests = 0,
      onlineFailures = 0,
      popupPages = 0,
      onlineActive = 0,
      httpErrors = 0,
      lastHttpStatus = 0;
    const context = await browser.newContext({ acceptDownloads: false, serviceWorkers: 'block' });
    // Legacy Samsung readers remove the cipher frame on Linux x86_64.
    // Present their supported Linux platform before any vendor script executes.
    if (['삼성', '현대', '우리'].includes(card))
      await context.addInitScript(() => {
        Object.defineProperty(navigator, 'platform', { get: () => 'Linux i686' });
      });
    // Offline by default. Opt-in official requests use a DNS-pinned transport.
    const official = 'https://samsungcard.com https://*.samsungcard.com';
    const initialCSP = allowOnline
      ? `connect-src ${official}; img-src data:; media-src 'none'; object-src 'none'; base-uri ${official}; form-action ${official}; frame-src ${official} about:`
      : "connect-src 'none'; img-src data:; media-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
    let delivered = false,
      blockedRequests = 0;
    await context.route('**/*', async (route) => {
      if (
        !delivered &&
        route.request().isNavigationRequest() &&
        route.request().url() === 'https://statement.invalid/'
      ) {
        delivered = true;
        return route.fulfill({
          contentType: 'text/html; charset=utf-8',
          body: payload.html,
          headers: { 'Content-Security-Policy': initialCSP },
        });
      }
      if (
        allowOnline &&
        allowedUrl(route.request().url()) &&
        !['image', 'media', 'font'].includes(route.request().resourceType()) &&
        onlineRequests < 200
      ) {
        onlineRequests++;
        onlineActive++;
        try {
          const response = await fetchSamsung(route.request(), { budget });
          lastHttpStatus = response.status;
          if (response.status >= 400) httpErrors++;
          return await route.fulfill(response);
        } catch {
          onlineFailures++;
          return route.abort().catch(() => {});
        } finally {
          onlineActive--;
        }
      }
      blockedRequests++;
      return route.abort().catch(() => {});
    });
    await context.routeWebSocket('**/*', (socket) => socket.close());
    let armed = false,
      written = '',
      writeCalls = 0,
      writeOverflow = false,
      unlockStarted = false,
      unlockState = null,
      cspBlocks = 0;
    await context.exposeBinding('__moaObserve', ({ frame }, event, value) => {
      if (!armed || frame !== page.mainFrame()) return;
      if (event === 'open') written = '';
      if (event === 'write' && typeof value === 'string') {
        writeCalls++;
        if (written.length + value.length > 2 * 1024 * 1024) writeOverflow = true;
        else written += value;
      }
      if (event === 'start') unlockStarted = true;
      if (event === 'end' && typeof value === 'boolean') unlockState = value;
      if (event === 'csp') cspBlocks++;
    });
    await context.addInitScript(() => {
      const report = (event, value) => window.__moaObserve(event, value).catch(() => {});
      for (const method of ['write', 'writeln', 'open']) {
        const original = Document.prototype[method];
        Document.prototype[method] = function (...args) {
          if (method === 'open') report('open');
          else report('write', args.map(String).join('') + (method === 'writeln' ? '\n' : ''));
          return original.apply(this, args);
        };
      }
      document.addEventListener('securitypolicyviolation', () => report('csp'));
    });
    const page = await context.newPage();
    context.on('page', (popup) => {
      if (popup === page) return;
      popupPages++;
      if (!allowOnline || context.pages().length > 4) {
        popup.close().catch(() => {});
        return;
      }
      popup.on('pageerror', () => scriptErrors++);
      popup.on('dialog', async (dialog) => {
        rejected = true;
        await dialog.dismiss();
      });
    });
    let rejected = false,
      scriptErrors = 0;
    page.on('pageerror', () => scriptErrors++);
    page.on('dialog', async (dialog) => {
      rejected = true;
      await dialog.dismiss();
    });
    async function captureStructure() {
      let best = page.mainFrame(),
        count = -1;
      for (const frame of context.pages().flatMap((p) => p.frames())) {
        const tables = await frame
          .locator('table')
          .count()
          .catch(() => 0);
        const dates = await frame
          .evaluate(extractList)
          .then((r) => r.dateNodes || 0)
          .catch(() => 0);
        const score = dates * 10000 + tables;
        if (score > count) {
          best = frame;
          count = score;
        }
      }
      return best
        .evaluate(() => {
          const root = document.documentElement.cloneNode(true);
          const live = [...document.documentElement.querySelectorAll('*')],
            copy = [...root.querySelectorAll('*')];
          for (let i = 0; i < Math.min(live.length, copy.length, 8000); i++) {
            const style = getComputedStyle(live[i]);
            if (style.display === 'none' || style.visibility === 'hidden')
              copy[i].setAttribute('data-moa-hidden', '1');
          }
          return root.outerHTML.slice(0, 2 * 1024 * 1024);
        })
        .catch(() => '');
    }
    await page.goto('https://statement.invalid/', { waitUntil: 'domcontentloaded', timeout: 8000 });
    const controls = await prepareStatementControls(page, card);
    if (!controls) {
      emit({ error: 'unsupported' });
      return;
    }
    const { password, confirm } = controls;
    // Inspect-only probe is used for readiness checks; no password guesses.
    if (payload.probe) {
      emit({ probe: true });
      return;
    }
    await page.evaluate(() => {
      window.__moaUnlockState = null;
      const action = window.doAction;
      if (typeof action === 'function')
        window.doAction = function (...args) {
          window.__moaObserve('start').catch(() => {});
          return action.apply(this, args);
        };
      for (const name of [
        'hubmail_onstart',
        'vestmail_onstart',
        'hubmail_onend',
        'vestmail_onend',
      ]) {
        const original = window[name];
        if (typeof original === 'function')
          window[name] = function (success, ...args) {
            if (name.endsWith('onstart')) window.__moaObserve('start').catch(() => {});
            if (typeof success === 'boolean') {
              window.__moaUnlockState = success;
              window.__moaObserve('end', success).catch(() => {});
            }
            return original.call(this, success, ...args);
          };
      }
    });
    await password.fill(payload.password);
    payload.password = '';
    armed = true;
    await confirm.click({ timeout: 3000, noWaitAfter: true });
    const deadline = Date.now() + (allowOnline ? 35000 : 12000);
    let stable = '',
      stableCount = 0,
      listDateNodes = 0,
      moreClicks = 0,
      expectedRecords = 0,
      readRecords = 0,
      hasExpected = false;
    while (Date.now() < deadline) {
      if (rejected) {
        emit({ error: 'unlock' });
        return;
      }
      const state =
        unlockState ?? (await page.evaluate(() => window.__moaUnlockState).catch(() => null));
      if (state === false) {
        emit({ error: 'unlock' });
        return;
      }
      if (onlineActive) {
        stableCount = 0;
        await new Promise((resolve) => setTimeout(resolve, 300));
        continue;
      }
      if (card === '삼성') {
        let expansion = {};
        for (const activePage of context.pages()) {
          expansion = await expandStatement(activePage, { allowOnline });
          if (expansion.clicked || expansion.stalled || expansion.waiting) break;
        }
        if (expansion.stalled || (expansion.clicked && ++moreClicks > 32)) {
          let observedExpected = 0,
            observedRead = 0,
            observedDates = 0;
          for (const frame of context.pages().flatMap((p) => p.frames())) {
            const observed = await frame.evaluate(extractList).catch(() => ({}));
            observedExpected += observed.expectedCount || 0;
            observedRead += observed.readCount || 0;
            observedDates += observed.dateNodes || 0;
          }
          emit({
            error: 'incomplete',
            rawStructure: await captureStructure(),
            diagnostics: {
              readerRevision: 9,
              moreClicks,
              expansionReason: expansion.reason || 3,
              expectedRecords: observedExpected,
              readRecords: observedRead,
              listDateNodes: observedDates,
              onlineRequests,
              onlineFailures,
              httpErrors,
              lastHttpStatus,
              onlineActive,
              popupPages,
            },
          });
          return;
        }
        if (expansion.clicked) {
          stableCount = 0;
          continue;
        }
        if (expansion.waiting) {
          stableCount = 0;
          await new Promise((resolve) => setTimeout(resolve, 300));
          continue;
        }
      }
      const candidates = [];
      listDateNodes = 0;
      expectedRecords = 0;
      readRecords = 0;
      hasExpected = false;
      let tableCount = 0;
      for (const frame of context.pages().flatMap((p) => p.frames())) {
        if (card === '롯데') {
          const lotte = await frame.evaluate(extractLotte).catch(() => ({ tables: [] }));
          if (lotte.oversized) {
            emit({ error: 'size' });
            return;
          }
          if (lotte.tables.length) {
            candidates.push(...lotte.tables);
            continue;
          }
        }
        const listed =
          card === '삼성'
            ? await frame.evaluate(extractList).catch(() => ({ tables: [] }))
            : { tables: [] };
        if (listed.oversized) {
          emit({ error: 'size' });
          return;
        }
        listDateNodes += listed.dateNodes || 0;
        if (listed.expectedCount !== null && listed.expectedCount !== undefined) {
          hasExpected = true;
          expectedRecords += listed.expectedCount;
          readRecords += listed.readCount || 0;
        }
        if (listed.tables.length) {
          candidates.push(...listed.tables);
          continue;
        }
        const extracted = await frame
          .evaluate(extractTables)
          .catch(() => ({ tables: [], tableCount: 0 }));
        if (extracted.oversized) {
          emit({ error: 'size' });
          return;
        }
        tableCount += extracted.tableCount;
        candidates.push(...extracted.tables);
        if (!extracted.tables.length && card !== '삼성') {
          const fallback = await frame.evaluate(extractList).catch(() => ({ tables: [] }));
          if (fallback.oversized) {
            emit({ error: 'size' });
            return;
          }
          listDateNodes += fallback.dateNodes || 0;
          candidates.push(...fallback.tables);
        }
      }
      const result = JSON.stringify(candidates);
      if (
        result.length > 2 * 1024 * 1024 ||
        candidates.some((t) => t.length > 20000 || t.some((r) => r.length > 100))
      ) {
        emit({ error: 'size' });
        return;
      }
      if (candidates.length && result === stable) stableCount++;
      else stableCount = 0;
      stable = result;
      if (stableCount >= 2) {
        if (hasExpected && readRecords !== expectedRecords) {
          await new Promise((resolve) => setTimeout(resolve, 300));
          continue;
        }
        emit({ tables: candidates, blockedRequests });
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
    const state =
      unlockState ?? (await page.evaluate(() => window.__moaUnlockState).catch(() => null));
    if (writeOverflow) {
      emit({ error: 'size' });
      return;
    }
    let writtenDiagnostics = {};
    if (written && state !== false && !rejected) {
      const recovered = await readWritten(browser, written, card);
      writtenDiagnostics = recovered.diagnostics || {};
      if (recovered.error) {
        emit({ error: recovered.error, diagnostics: { readerRevision: 9, writeCalls } });
        return;
      }
      if (!allowOnline && recovered.tables.length) {
        emit({ tables: recovered.tables, blockedRequests });
        return;
      }
    }
    const frames = context.pages().flatMap((p) => p.frames()).length;
    let tableCount = 0;
    for (const frame of context.pages().flatMap((p) => p.frames()))
      tableCount += await frame
        .locator('table')
        .count()
        .catch(() => 0);
    const code = allowOnline
      ? hasExpected && readRecords !== expectedRecords
        ? 'incomplete'
        : 'online_failed'
      : writtenDiagnostics.externalDestinations > 0
        ? 'online_required'
        : hasExpected && readRecords !== expectedRecords
          ? 'incomplete'
          : state === true
            ? 'unlocked_no_table'
            : scriptErrors
              ? 'script_error'
              : 'no_table';
    emit({
      error: code,
      rawStructure:
        code === 'incomplete' || (['현대', '우리'].includes(card) && unlockStarted)
          ? await captureStructure()
          : undefined,
      diagnostics: {
        httpErrors,
        lastHttpStatus,
        onlineActive,
        onlineRequests,
        onlineFailures,
        popupPages,
        ...writtenDiagnostics,
        readerRevision: 9,
        unlockStarted: Number(unlockStarted),
        unlockEnded: state === true ? 1 : 0,
        writeCalls,
        writtenChars: written.length,
        passwordFields: await page
          .locator('#password')
          .count()
          .catch(() => 0),
        cspBlocks,
        frames,
        tables: tableCount,
        scriptErrors,
        blockedRequests,
        listDateNodes,
        expectedRecords,
        readRecords,
        moreClicks,
        unlockConfirmed: state === true,
      },
    });
  } catch (_) {
    emit({ error: browser ? 'processing' : 'browser' });
  } finally {
    payload.password = '';
    if (browser) await browser.close();
    clearTimeout(timer);
  }
})().catch(() => {
  emit({ error: 'processing' });
  process.exitCode = 1;
});
