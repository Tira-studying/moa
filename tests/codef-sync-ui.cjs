/** 브라우저 없이 실제 개인 CODEF 화면의 조회·검토 요청과 버전 분류를 확인합니다. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
(async () => {
  const elements = new Map();
  for (const id of [
    'codef-sync-state',
    'codef-sync-links',
    'codef-sync-review',
    'codef-sync-panel',
    'codef-sync-refresh',
    'codef-sync-request',
    'sharing',
  ]) {
    elements.set(id, {
      textContent: '',
      innerHTML: '',
      listeners: {},
      hidden: false,
      open: false,
      addEventListener(type, callback) {
        this.listeners[type] = callback;
      },
    });
  }
  const calls = [];
  const context = vm.createContext({
    $: (id) => elements.get(id),
    Date,
    document: { hidden: false },
    setInterval: () => {},
    notify: () => {},
    escapeHTML: (value) =>
      String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;'),
    won: (value) => String(value),
    api: async (path, options = {}) => {
      calls.push({ path, options });
      return {
        worker_running: true,
        schedule: '가상 회차',
        links: [
          {
            card: 'samsung',
            enabled: true,
            state: 'ready',
            message: '가상 상태',
            saved_count: 1,
            review_count: 1,
          },
        ],
        manual: [
          { card: 'lotte', last_success: 1, group_count: 2, saved_count: 1, review_count: 1 },
        ],
        review: [
          {
            id: 1,
            card: 'samsung',
            used_on: '2026-01-01',
            merchant: '<script>fixture</script>',
            net: 1200,
            reason: '명세서와 중복 후보',
            kind: '0',
          },
          {
            id: 2,
            card: 'samsung',
            used_on: '2026-01-01',
            merchant: '가상 검토 상점',
            net: null,
            reason: '동일 승인 식별자의 이용금액 차이 확인 필요',
            kind: 'mixed',
          },
        ],
      };
    },
  });
  vm.runInContext(fs.readFileSync('static/codef-sync.js', 'utf8'), context);
  assert.equal(calls.length, 0);
  elements.get('codef-sync-panel').open = true;
  await elements.get('codef-sync-panel').listeners.toggle();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls[0].path, '/api/codef-sync');
  assert.match(elements.get('codef-sync-links').innerHTML, /롯데 · 수동 조회·저장/);
  assert.match(elements.get('codef-sync-links').innerHTML, /예약 수집 연결을 만들지 않습니다/);
  assert.match(elements.get('codef-sync-review').innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(elements.get('codef-sync-review').innerHTML, /<script>/);
  assert.match(elements.get('codef-sync-review').innerHTML, /금액 확인 필요/);
  assert.doesNotMatch(elements.get('codef-sync-review').innerHTML, /data-codef-save="2"/);
  assert.match(elements.get('codef-sync-review').innerHTML, /data-codef-ignore="2"/);
  const button = { dataset: { codefSave: '1' }, disabled: false };
  await elements.get('codef-sync-panel').listeners.click({ target: { closest: () => button } });
  const mutation = calls.find((call) => call.options.method === 'POST');
  assert.equal(mutation.path, '/api/codef-sync/review/1');
  assert.deepEqual(JSON.parse(mutation.options.body), { action: 'save' });
  const source = fs.readFileSync('static/app.js', 'utf8');
  const start = source.indexOf('function revisionResource(path)');
  const end = source.indexOf('\n}', start) + 2;
  vm.runInContext(source.slice(start, end), context);
  assert.equal(
    vm.runInContext('revisionResource("/api/codef-sync/review/1")', context),
    'codefSync',
  );
  assert.equal(vm.runInContext('revisionResource("/api/codef-sync")', context), 'codefSync');
  console.log('CODEF 개인 화면 조회·검토·버전 연결: OK');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
