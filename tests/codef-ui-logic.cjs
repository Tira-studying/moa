/** 브라우저 없이 실제 CODEF 화면 코드의 요청 순서·버튼 상태·오류 유지·늦은 응답을 검증합니다. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
(async () => {
  const elements = new Map();
  for (const id of ['sharing', 'codef-settings', 'codef-state', 'codef-check', 'codef-refresh'])
    elements.set(id, { hidden: id === 'sharing', textContent: '', disabled: false });
  const calls = [];
  let configured = true;
  let postCount = 0;
  let nextGet;
  const context = vm.createContext({
    $: (id) => elements.get(id),
    Date,
    notify: () => {},
    api: async (path, options = {}) => {
      calls.push({ path, method: options.method || 'GET' });
      if (options.method === 'POST') {
        assert.equal(options.body, undefined);
        if (++postCount > 1) throw new Error('인증 확인은 1분에 한 번 가능합니다.');
        return { configured: true, ok: true, message: '개발자 인증 성공', checked_at: 1730000000 };
      }
      if (nextGet) return nextGet;
      return { configured, ok: null, message: configured ? '설정 정상' : '서버 키 설정 필요' };
    },
  });
  vm.runInContext(fs.readFileSync('static/codef.js', 'utf8'), context);
  assert.equal(calls.length, 0);
  await vm.runInContext('loadCodefStatus()', context);
  assert.deepEqual(calls, [{ path: '/api/codef/status', method: 'GET' }]);
  assert.equal(elements.get('codef-check').disabled, false);
  await elements.get('codef-check').onclick();
  assert.match(elements.get('codef-state').textContent, /개발자 인증 성공/);
  assert.equal(elements.get('codef-check').disabled, false);
  await elements.get('codef-check').onclick();
  assert.match(elements.get('codef-state').textContent, /1분에 한 번/);
  configured = false;
  await elements.get('codef-refresh').onclick();
  assert.equal(elements.get('codef-check').disabled, true);
  let resolve;
  nextGet = new Promise((done) => (resolve = done));
  const old = vm.runInContext('loadCodefStatus()', context);
  nextGet = undefined;
  await vm.runInContext('loadCodefStatus()', context);
  resolve({ configured: true, message: '오래된 응답' });
  await old;
  assert.match(elements.get('codef-state').textContent, /서버 키 설정 필요/);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
