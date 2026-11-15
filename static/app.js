/**
 * 화면 공통 기반: 공통 도구($, won, escapeHTML), 메모리 상태(state), 탭 이동, 서버 통신(api),
 * 소비 대시보드 그리기를 담당합니다. templates/index.html의 id/data 속성과 연결됩니다.
 * 화면별 기능은 transactions.js(사용 내역) → imports.js(파일 추가·검토) → mail.js(이메일)로 나눴고,
 * 이 파일의 $, api, state를 그 파일들과 finance.js · billing.js · sharing.js가 함께 사용하므로
 * index.html의 script defer 순서를 유지합니다.
 */

// 1. 공통 화면 도구와 메모리 상태
// ─────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const cards = ['신한', '삼성', '현대', 'KB국민', '우리', '롯데'];
const colors = ['#e9effc', '#e8f0fc', '#f0edf7', '#fbf4df', '#e5f4f9', '#f9e9eb'];
let state = { transactions: [], imports: [] },
  preview = null,
  limit = 20,
  // 사용 내역 보기 방식('list' | 'calendar')과 달력에서 고른 날짜(YYYY-MM-DD, 없으면 '')
  activityMode = 'list',
  calendarDay = '',
  toastTimer;
const won = (n) => new Intl.NumberFormat('ko-KR').format(n);
const escapeHTML = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
/** 개인 표시 이름은 식별자와 분리합니다. 이름을 바꿔도 저장된 카드사 키는 유지합니다. */
function managementCardName(row) {
  const name = row.display_name || row.card;
  return name === row.card ? row.card : `${name} (${row.card})`;
}
const localMonth = () => MoaAnalytics.todayKST().slice(0, 7);
let reviewOrigin = 'upload',
  historyLimit = 10;
const viewTitles = {
  dashboard: '대시보드',
  activity: '사용 내역',
  'mail-inbox': '이메일 명세서',
  upload: '파일 추가',
  history: '저장 기록',
  planning: '지출 관리',
  sharing: '계정 · 공동 관리',
  bank: '계좌 입출금',
  connections: '카드·계좌 연결',
  sms: '실시간 소비 · 문자',
  review: '명세서 검토',
};
// 2. 화면 이동과 탭 전환
// ─────────────────────────────────────────────────────────────────
/** 파일 추가 화면에서 CSV·엑셀과 보안 명세서 입력 영역을 전환합니다. */
function setUploadMode(mode) {
  document
    .querySelectorAll('[data-upload-panel]')
    .forEach((panel) => (panel.hidden = panel.dataset.uploadPanel !== mode));
  document.querySelectorAll('[data-upload-mode]').forEach((button) => {
    const active = button.dataset.uploadMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}
/** 주소의 #탭 값에 맞춰 한 화면만 표시하고 메뉴·제목·조회 동작을 갱신합니다. */
function navigateTo(target, history = true) {
  const element = $(target);
  let view = element?.closest('[data-view]')?.dataset.view || 'dashboard';
  if (
    view === 'review' &&
    $('mapping').classList.contains('hidden') &&
    $('secure-selection').classList.contains('hidden')
  )
    view = 'upload';
  if (target === 'secure-upload') setUploadMode('secure');
  document
    .querySelectorAll('[data-view]')
    .forEach((panel) => (panel.hidden = panel.dataset.view !== view));
  document.querySelectorAll('aside .nav,.mobile-nav a').forEach((link) => {
    const active =
      link.hash ===
      '#' +
        (view === 'review'
          ? reviewOrigin
          : ['bank', 'connections'].includes(view)
            ? 'sharing'
            : view);
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  $('page-title').textContent = viewTitles[view];
  document.body.dataset.currentView = view;
  if (view === 'sms' && typeof loadSms === 'function') loadSms();
  if (view === 'bank' && typeof loadBank === 'function' && loadBank.ready) loadBank();
  if (
    view === 'connections' &&
    typeof loadConnectionSetup === 'function' &&
    loadConnectionSetup.ready
  )
    loadConnectionSetup();
  if (view !== 'connections' && typeof clearConnectionInputs === 'function')
    clearConnectionInputs();
  if (
    view === 'dashboard' &&
    !$('personal-dashboard').hidden &&
    typeof loadSmsDashboard === 'function'
  )
    loadSmsDashboard();
  if (view !== 'sms' && typeof clearSmsKey === 'function') clearSmsKey();
  if (view === 'planning' && typeof loadFinance === 'function') loadFinance();
  if (view === 'sharing' && typeof loadMembers === 'function') loadMembers();
  if (view === 'dashboard' && typeof loadBillingSummary === 'function') loadBillingSummary();
  if (view === 'dashboard' && typeof loadCardPerformanceSummary === 'function')
    loadCardPerformanceSummary();
  if (view === 'dashboard' && typeof loadPlanningDashboard === 'function') loadPlanningDashboard();
  if (
    view === 'dashboard' &&
    typeof loadHousehold === 'function' &&
    !$('household-dashboard').hidden
  )
    loadHousehold();
  if (history && location.hash !== '#' + view) window.history.pushState(null, '', '#' + view);
  window.scrollTo({ top: 0, behavior: 'auto' });
}
document.addEventListener('click', (event) => {
  const link = event.target.closest('a[href^="#"]');
  if (!link || !$(link.hash.slice(1))) return;
  event.preventDefault();
  navigateTo(link.hash.slice(1));
});
window.addEventListener('popstate', () => navigateTo(location.hash.slice(1), false));
window.addEventListener('hashchange', () => navigateTo(location.hash.slice(1), false));
document
  .querySelectorAll('[data-upload-mode]')
  .forEach((button) => (button.onclick = () => setUploadMode(button.dataset.uploadMode)));
$('review-back').onclick = () => {
  if (document.body.dataset.reviewStep === 'mapping' && secureResult) {
    $('mapping').classList.add('hidden');
    document.body.dataset.reviewStep = 'tables';
    navigateTo('review');
  } else navigateTo(reviewOrigin === 'mail-inbox' ? 'mail-inbox' : 'upload');
};
navigateTo(location.hash.slice(1), false);
$('month').value = localMonth();
$('tx-month').value = localMonth();
// 3. 메시지 표시와 서버 통신
// ─────────────────────────────────────────────────────────────────
function clearNotice() {
  clearTimeout(toastTimer);
  $('status').className = 'hidden';
}
/** 성공 또는 오류 메시지를 잠시 표시합니다. 민감한 본문은 메시지로 전달하지 않습니다. */
function notify(message, error = false) {
  clearTimeout(toastTimer);
  $('status').textContent = message;
  $('status').className = error ? 'error' : '';
  toastTimer = setTimeout(() => ($('status').className = 'hidden'), error ? 18000 : 6000);
}
const resourceRevisions = {};
/** API 경로를 화면 자료 종류에 연결해 어떤 조회 버전으로 저장할지 결정합니다. */
function revisionResource(path) {
  if (path.startsWith('/api/bank')) return 'bank';
  if (path.startsWith('/api/categories')) return 'categories';
  if (path.startsWith('/api/category-reviews')) return 'categoryReviews';
  if (path.startsWith('/api/codef-sync')) return 'codefSync';
  if (path.startsWith('/api/sms')) return 'sms';
  if (path.startsWith('/api/billing')) return 'billing';
  if (path.startsWith('/api/card-performance')) return 'cardPerformance';
  if (
    path.startsWith('/api/features') ||
    path.startsWith('/api/budgets') ||
    path.startsWith('/api/tax-deduction') ||
    path.startsWith('/api/subscriptions')
  )
    return 'planningTools';
  if (path.startsWith('/api/members') || path.startsWith('/api/account')) return 'members';
  if (
    path.startsWith('/api/finance') ||
    path.startsWith('/api/installments') ||
    /\/(exclusion|recurring)$/.test(path)
  )
    return 'finance';
  if (
    path === '/api/data' ||
    path.startsWith('/api/transactions') ||
    path.startsWith('/api/import')
  )
    return 'ledger';
  return null;
}
/** 공통 서버 요청: 인증·위조 요청 방지 헤더와 화면 버전을 보내고 오류 응답을 처리합니다.
 * 반환 객체의 _revision은 화면 편집 시점 확인용이며 JSON 본문 데이터와는 구분합니다. */
async function api(path, options = {}) {
  const resource = revisionResource(path),
    writing = ['POST', 'PUT', 'DELETE', 'PATCH'].includes(options.method);
  const revision = Object.prototype.hasOwnProperty.call(options, 'revision')
    ? options.revision
    : resourceRevisions[resource];
  const { revision: unused, ...fetchOptions } = options;
  const response = await fetch(path, {
    ...fetchOptions,
    headers: {
      'X-Requested-With': 'CardDashboard',
      ...(/^\d+$/.test(document.body.dataset.userId || '')
        ? { 'X-Moa-User': document.body.dataset.userId }
        : {}),
      ...(writing && revision != null ? { 'If-Match': String(revision) } : {}),
      ...options.headers,
    },
  });
  if (response.status === 401) {
    location.replace('/login');
    throw new Error('로그인이 필요합니다.');
  }
  const data = await response.json();
  if (!response.ok) {
    if (['revision_conflict', 'revision_required', 'account_changed'].includes(data.code))
      $('conflict-notice').hidden = false;
    const error = new Error(data.error || '요청에 실패했습니다. 다시 시도해 주세요.');
    error.structure = data.structure;
    throw error;
  }
  const received = response.headers.get('X-Moa-Revision');
  if (response.ok && received !== null) {
    Object.defineProperty(data, '_revision', { value: received });
    if (!writing && resource) resourceRevisions[resource] = received;
  }
  return data;
}
$('reload-shared').onclick = () => location.reload();
// 4. 거래 조회와 소비 대시보드
// ─────────────────────────────────────────────────────────────────
/** 저장된 거래와 파일 목록을 다시 받아 화면 상태와 분석을 갱신합니다. */
async function refresh() {
  state = await api('/api/data');
  const activeTransactions = state.transactions.filter((row) => !row.excluded);
  if (
    !$('demo').checked &&
    activeTransactions.length &&
    !activeTransactions.some((t) => t.used_on.startsWith($('month').value))
  ) {
    $('month').value = activeTransactions[0].used_on.slice(0, 7);
  }
  const selected = $('filter-category').value;
  const selectedPayment = $('filter-card').value;
  $('filter-card').innerHTML =
    '<option value="">전체 카드·계좌</option>' +
    [...new Set([...cards, ...state.transactions.map((row) => row.card)])]
      .map((name) => `<option>${escapeHTML(name)}</option>`)
      .join('');
  $('filter-card').value = [...$('filter-card').options].some(
    (item) => item.value === selectedPayment,
  )
    ? selectedPayment
    : '';
  $('filter-category').innerHTML =
    '<option value="">전체 분야</option>' +
    categoryNames()
      .map((name) => `<option>${escapeHTML(name)}</option>`)
      .join('');
  if (categoryNames().includes(selected)) $('filter-category').value = selected;
  render();
  renderImports();
  if (typeof loadFinance === 'function' && !$('planning').hidden) await loadFinance();
}
/** 원본 거래를 저장하지 않는 화면 체험용 가상 데이터를 만듭니다. */
function demoData() {
  const month = $('month').value;
  const shops = [
    '동네 마트',
    '오후의 커피',
    '교보문고',
    '주말 식탁',
    '도시철도',
    '생활용품점',
    '온라인 쇼핑',
    '주유소',
    '베이커리',
    '영화관',
    '휴대폰 요금',
    '점심 한 끼',
  ];
  const categories = [
    '마트·식료품',
    '커피',
    '문화·여가',
    '식비',
    '교통',
    '주거·생활',
    '쇼핑',
    '차량·주유',
    '식비',
    '문화·여가',
    '통신·공과금',
    '식비',
  ];
  return shops.map((merchant, i) => ({
    category: categories[i],
    id: i,
    used_on: `${month}-${String(24 - i).padStart(2, '0')}`,
    merchant,
    card: cards[i % 6],
    amount: [63400, 5800, 28000, 46000, 62000, 19000, 128000, 70000, 12400, 15000, 55000, -12000][
      i
    ],
  }));
}
/** 선택한 월의 거래를 골라 대시보드 합계와 소비 패턴을 그립니다. */
function render() {
  const demo = $('demo').checked;
  $('demo-banner').classList.toggle('hidden', !demo);
  const all = demo ? demoData() : state.transactions.filter((row) => !row.excluded);
  const comparison = MoaAnalytics.comparePeriods(
    all,
    $('month').value,
    demo ? $('month').value + '-28' : MoaAnalytics.todayKST(),
  );
  const current = comparison.current;
  const total = current.reduce((s, t) => s + t.amount, 0);
  $('total').innerHTML = `${won(total)}<span>원</span>`;
  $('card-count').innerHTML =
    `${new Set(current.filter((t) => t.payment_kind === 'card').map((t) => t.card)).size}<span>개</span>`;
  $('tx-count').innerHTML = `${current.length}<span>건</span>`;
  const refunds = current.filter((t) => t.amount < 0);
  $('refunds').textContent = refunds.length
    ? `취소·환불 ${refunds.length}건 · ${won(-refunds.reduce((s, t) => s + t.amount, 0))}원 차감`
    : '취소·환불 내역 없음';
  renderDashboardComparison(all, $('month').value, comparison);
  const cutoff = `${$('month').value}-${String(comparison.cutoff).padStart(2, '0')}`;
  renderPatterns(
    all.filter((row) => !row.used_on.startsWith($('month').value) || row.used_on <= cutoff),
    $('month').value,
  );
  if (!$('household-dashboard').hidden && typeof loadHousehold === 'function') {
    $('household-month').value = $('month').value;
    loadHousehold();
  }
  renderTransactions();
}
let patternTab = 'categories';
function categoryNames() {
  return (
    state.categories || [
      '식비',
      '커피',
      '의료',
      '마트·식료품',
      '편의점',
      '교통',
      '차량·주유',
      '쇼핑',
      '문화·여가',
      '교육',
      '육아',
      '구독료',
      '통신·공과금',
      '주거·생활',
      '여행·숙박',
      '보험·금융',
      '기타',
      '미분류',
    ]
  );
}
/** 숫자 자료 하나를 접근 가능한 막대 표시용 HTML로 바꿉니다. */
function patternBar(label, amount, max, attributes = '', detail = '') {
  const width = max ? Math.max(amount === 0 ? 0 : 1, (Math.abs(amount) / max) * 100) : 0;
  return /* HTML */ `<button type="button" class="pattern-row" ${attributes}>
    <span class="pattern-label">${escapeHTML(label)}</span
    ><span class="pattern-track"
      ><span
        class="pattern-fill ${amount < 0 ? 'negative' : ''}"
        style="width:${width}%"
      ></span></span
    ><span class="pattern-amount">${escapeHTML(detail || won(amount) + '원')}</span>
  </button>`;
}
/** 계산 모듈의 결과를 소비 분야·추이·요일 그래프로 표시합니다. */
function renderPatterns(all, month) {
  const report = MoaAnalytics.summarize(all, month);
  const unknown = all.filter(
    (row) => row.used_on.startsWith(month) && (row.category || '미분류') === '미분류',
  ).length;
  $('category-status').textContent = unknown
    ? `미분류 ${unknown}건이 있습니다. ‘분류 수정’에서 분야를 지정해 주세요.`
    : '자동 분류는 추정값입니다. 사용 내역에서 확인하고 수정할 수 있어요.';
  $('pattern-average').textContent = won(report.average) + '원';
  $('pattern-days').textContent = report.activeDays + '일';
  $('pattern-weekend').textContent = report.weekendShare + '%';
  renderDashboardTrend(report.trend);
  const items = patternTab === 'cards' ? report.netCards : report[patternTab],
    highest = Math.max(0, ...items.map((item) => Math.abs(item.amount)));
  $('pattern-title').textContent = {
    categories: '분야별 소비',
    cards: '카드·계좌별 순사용액',
    merchants: '가맹점 TOP 5',
    weekdays: '요일별 소비',
  }[patternTab];
  $('pattern-distribution').innerHTML = (patternTab === 'cards' ? items.length : report.gross)
    ? items
        .map((item) =>
          patternBar(
            item.name,
            item.amount,
            highest,
            patternTab === 'categories'
              ? `data-pattern-category="${escapeHTML(item.name)}"`
              : patternTab === 'cards'
                ? `data-pattern-card="${escapeHTML(item.name)}"`
                : patternTab === 'merchants'
                  ? `data-pattern-merchant="${escapeHTML(item.name)}"`
                  : 'disabled',
            patternTab === 'categories'
              ? won(item.amount) + '원 · ' + item.share + '%'
              : patternTab === 'merchants'
                ? won(item.amount) + '원 · ' + item.count + '건'
                : won(item.amount) + '원',
          ),
        )
        .join('')
    : '<p class="help">이 달에는 취소·환불을 제외한 사용 내역이 없습니다.</p>';
  $('pattern-caption').textContent =
    patternTab === 'categories'
      ? '가맹점 이름으로 분류한 사용 금액 비중입니다. 취소·환불은 제외하며, 분야를 누르면 관련 내역으로 이동합니다.'
      : patternTab === 'cards'
        ? '취소·환불을 차감한 카드별 순사용액입니다. 카드를 누르면 해당 월 내역으로 이동합니다.'
        : patternTab === 'merchants'
          ? '같은 이름의 가맹점을 합산합니다. 누르면 해당 월 내역을 검색합니다.'
          : '요일별 사용 금액 합계입니다. 취소·환불은 제외합니다.';
}
