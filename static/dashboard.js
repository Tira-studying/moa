/**
 * 대시보드의 기간 비교·누적 그래프·세로 추이·신규 분류 확인을 표시합니다.
 * 개인 거래와 확인 상태는 서버에서 읽으며 브라우저 저장소나 통합 화면에 복제하지 않습니다.
 * 함수는 공통 app.js의 $, api, state가 준비된 뒤 호출합니다.
 */
let dashboardBillingMonth = null;
let categoryReviewVisibleIds = [];
let dashboardBankSummary = null;

function updateDashboardAlerts() {
  $('dashboard-alerts').hidden =
    $('open-category-review').hidden &&
    $('open-bank-review').hidden &&
    $('open-budget-alert').hidden;
}

/** 선택한 계좌 소비가 새 카드 후보로 보류된 경우에만 확인을 안내합니다. */
function renderBankReviewAlert(summary) {
  dashboardBankSummary = summary;
  const count = summary?.pending_count || 0;
  $('open-bank-review').hidden = !count || $('demo').checked;
  $('bank-dashboard-summary').textContent =
    `선택한 계좌 소비 ${won(count)}건 반영 보류 · 카드 중복을 확인해 주세요`;
  updateDashboardAlerts();
}

/** 별도 금융 요청 없이 이미 저장된 신규 거래의 확인 필요 건수만 안내합니다. */
function renderCategoryReviewAlert(count) {
  $('open-category-review').hidden = !count || $('demo').checked;
  $('category-review-summary').textContent =
    `새 사용내역 ${won(count)}건 · 소비 분야를 확인해 주세요`;
  updateDashboardAlerts();
}

/** SVG 좌표는 검증한 숫자로만 만들고 음수 환불도 같은 축에 표현합니다. */
function renderDashboardComparison(all, month, comparison) {
  const previous = comparison.previousTotal;
  const difference = comparison.currentTotal - previous;
  $('dashboard-title').textContent = `${Number(month.slice(5))}월 지출`;
  $('spending-period').textContent =
    `${Number(month.slice(5))}/1~${comparison.cutoff}${comparison.inProgress ? ' · 진행 중' : ''}`;
  $('comparison').textContent = $('demo').checked
    ? '가상 예시 · 실제 내역이 아닙니다'
    : comparison.future
      ? '미래 월 · 전월 비교 보류'
      : !comparison.hasPrevious
        ? '전월 등록 내역 없음 · 비교 보류'
        : `${comparison.inProgress ? '전월 같은 기간' : '전월 전체'} 대비 ${difference === 0 ? '동일' : `${won(Math.abs(difference))}원 ${difference > 0 ? '증가' : '감소'}`}${previous ? ` (${difference > 0 ? '+' : ''}${Math.round((difference / Math.abs(previous)) * 100)}%)` : ''}`;
  const values = [0, ...comparison.currentSeries, ...comparison.previousSeries];
  const low = Math.min(...values),
    high = Math.max(...values),
    range = high - low || 1;
  const days = Math.max(comparison.days, comparison.previousDays);
  const x = (day) => 12 + (day / days) * 456;
  const y = (value) => 104 - ((value - low) / range) * 88;
  const path = (series) =>
    [[x(0), y(0)], ...series.map((value, index) => [x(index + 1), y(value)])]
      .map(([left, top], index) => `${index ? 'L' : 'M'}${left.toFixed(2)},${top.toFixed(2)}`)
      .join(' ');
  $('comparison-chart').innerHTML =
    `<svg viewBox="0 0 480 130" role="img" aria-label="이번 달과 지난달 등록 내역의 누적 순사용액">
    <line x1="12" x2="468" y1="${y(0)}" y2="${y(0)}" stroke="#e9ecf0" />
    ${comparison.hasPrevious ? `<path d="${path(comparison.previousSeries)}" fill="none" stroke="#9ca4ae" stroke-width="2" stroke-dasharray="5 5" />` : ''}
    <path d="${path(comparison.currentSeries)}" fill="none" stroke="#18786b" stroke-width="3" />
    <circle cx="${x(comparison.cutoff)}" cy="${y(comparison.currentTotal)}" r="4" fill="#18786b" />
    <text x="12" y="126">1일</text><text x="230" y="126">15일</text><text x="440" y="126">월말</text></svg>`;
  $('comparison-legend').innerHTML =
    '<span>━ 이번 달 누적</span><span>┄ 지난달 누적 · 월 전체</span>';
  const current = comparison.current;
  $('recent-transactions').innerHTML =
    current
      .slice()
      .sort((a, b) => b.used_on.localeCompare(a.used_on) || b.id - a.id)
      .slice(0, 3)
      .map(
        (row) =>
          `<button type="button" class="recent-row" data-recent-id="${row.id}"><time>${escapeHTML(row.used_on.slice(5).replace('-', '/'))}</time><span>${escapeHTML(row.actual_merchant || row.merchant)}<small>${escapeHTML(row.category || '미분류')} · ${escapeHTML(row.card)}</small></span><strong class="${row.amount < 0 ? 'refund' : ''}">${won(row.amount)}원</strong></button>`,
      )
      .join('') || '<p class="help">이 달의 저장된 사용 내역이 없습니다.</p>';
  renderCategoryReviewAlert(
    state.transactions.filter((row) => !row.excluded && row.category_reviewed === false).length,
  );
  renderBankReviewAlert(state.bank_review_summary);
  if (dashboardBillingMonth !== month && typeof loadBillingSummary === 'function') {
    dashboardBillingMonth = month;
    loadBillingSummary();
  }
}

function renderDashboardTrend(items) {
  const positive = Math.max(0, ...items.map((item) => item.amount));
  const negative = Math.abs(Math.min(0, ...items.map((item) => item.amount)));
  const range = positive + negative || 1;
  const baseline = (negative / range) * 100;
  const todayMonth = MoaAnalytics.todayKST().slice(0, 7);
  $('pattern-trend').innerHTML = items
    .map((item) => {
      const progress = item.month === todayMonth;
      const missing = !item.count;
      const status = missing ? '미등록' : progress ? '진행 중' : '등록';
      const height = (Math.abs(item.amount) / range) * 100;
      const position = missing
        ? 'bottom:0'
        : item.amount < 0
          ? `top:${100 - baseline}%`
          : `bottom:${baseline}%`;
      return `<button class="trend-column ${missing ? 'missing' : ''} ${progress ? 'current' : ''}" type="button" data-pattern-month="${item.month}" aria-label="${item.month} ${status}${missing ? '' : ` ${won(item.amount)}원`}"><span class="trend-value">${missing ? '—' : won(item.amount)}</span><span class="trend-track"><span class="trend-zero" style="bottom:${baseline}%"></span><span class="trend-fill ${item.amount < 0 ? 'negative' : ''}" style="height:${missing ? 100 : height}%;${position}"></span></span><span>${Number(item.month.slice(5))}월</span><small>${missing ? '미등록' : progress ? '진행 중' : ''}</small></button>`;
    })
    .join('');
}

/** 화면이 열린 동안에는 건수만 읽고, 클릭한 시점에 실제 신규 내역을 새로 읽습니다. */
async function pollCategoryReviews() {
  if (
    document.visibilityState !== 'visible' ||
    $('dashboard').hidden ||
    $('personal-dashboard').hidden ||
    $('demo').checked
  )
    return;
  try {
    const data = await api('/api/category-reviews');
    renderCategoryReviewAlert(data.pending_count);
    renderBankReviewAlert(data.bank_review_summary);
  } catch (_) {
    // 연결 상태는 공통 PWA 안내가 맡으며 기존 안내를 지우거나 확인 완료로 간주하지 않습니다.
  }
}

document.addEventListener('DOMContentLoaded', () => {
  $('open-bank-review').onclick = () => {
    if (dashboardBankSummary?.pending_month)
      $('bank-month').value = dashboardBankSummary.pending_month;
    $('bank-review-filter').value = 'pending';
    navigateTo('bank');
  };
  $('open-category-review').onclick = async () => {
    try {
      await refresh();
      openActivity({ reviewOnly: true });
    } catch (error) {
      notify(error.message, true);
    }
  };
  $('confirm-category-reviews').onclick = async () => {
    const ids = categoryReviewVisibleIds.slice(0, 500);
    if (!ids.length) return;
    $('confirm-category-reviews').disabled = true;
    try {
      await api('/api/category-reviews', {
        method: 'PUT',
        revision: state._revision,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      await refresh();
      notify(`${ids.length}건의 소비 분야를 확인했습니다.`);
    } catch (error) {
      notify(error.message, true);
      renderTransactions();
    }
  };
  $('tx-review-only').onchange = () => {
    limit = 20;
    renderTransactions();
  };
  $('recent-all').onclick = () => openActivity({ month: $('month').value });
  $('recent-transactions').onclick = (event) => {
    const button = event.target.closest('[data-recent-id]');
    if (!button) return;
    if ($('demo').checked) return notify('가상 예시입니다. 실제 내역은 사용 내역에서 확인하세요.');
    const row = state.transactions.find((item) => item.id === Number(button.dataset.recentId));
    if (row) openTransactionEditor(row);
  };
  $('account-logout-button').onclick = () => $('logout-button').click();
  setInterval(pollCategoryReviews, 30000);
  document.addEventListener('visibilitychange', pollCategoryReviews);
});
