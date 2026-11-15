/**
 * 예산·연말정산·정기 결제 화면: 설정의 ‘화면 기능 켜기 · 끄기’, 지출 관리의 ‘예산’·‘소득공제’ 탭,
 * 고정 지출 탭의 구독 요약, 대시보드 요약과 예산 알림을 담당합니다.
 * 계산은 서버(budgets.py, tax_deduction.py, subscriptions.py)가 맡고 여기서는 표시와 입력만 합니다.
 * 꺼진 기능은 탭·요약을 숨기고 API도 부르지 않습니다. 카드 실적 켜기·끄기는 card-performance.js가 맡습니다.
 * app.js의 $, api, won, escapeHTML, notify, localMonth와 finance.js의 showFinanceTab, stat을 사용합니다.
 */

let planningFeatures = { budgets: false, tax_deduction: false, subscriptions: false },
  budgetData = null,
  taxData = null;

const planningFeatureNames = {
  budgets: '분야별 예산',
  tax_deduction: '연말정산 카드 공제',
  subscriptions: '구독·정기 결제',
  share_transactions: '내 사용 내역 함께 보기',
};
const budgetStatusText = { over: '초과', warn: '80% 이상', pace: '빠르게 쓰는 중', ok: '' };

/** "50,000,000"처럼 쉼표·원이 섞인 입력을 정수로 바꿉니다. 빈칸은 0입니다. */
function parseWon(value) {
  const text = String(value || '').replace(/[,\s원]/g, '');
  if (!text) return 0;
  if (!/^\d+$/.test(text)) throw new Error('금액은 숫자로 입력해 주세요.');
  return Number(text);
}

// 1. 기능 켜기·끄기
// ─────────────────────────────────────────────────────────────────
/** 켜진 기능만 탭·요약을 보이게 합니다. 보고 있던 탭의 기능을 끄면 기본 탭(할부 일정)으로 돌아갑니다. */
function applyPlanningVisibility() {
  const tabs = { budgets: 'budgets', tax_deduction: 'tax' };
  for (const [feature, tab] of Object.entries(tabs)) {
    const enabled = planningFeatures[feature];
    document.querySelector(`[data-finance-tab="${tab}"]`).hidden = !enabled;
    if (!enabled && !document.querySelector(`[data-finance-panel="${tab}"]`).hidden)
      showFinanceTab('installments');
  }
  document
    .querySelectorAll('[data-feature]')
    .forEach((input) => (input.checked = Boolean(planningFeatures[input.dataset.feature])));
  if (!planningFeatures.budgets) {
    $('dashboard-budget').hidden = true;
    $('open-budget-alert').hidden = true;
    updateDashboardAlerts();
  }
  if (!planningFeatures.tax_deduction) $('dashboard-tax').hidden = true;
  if (!planningFeatures.subscriptions) {
    $('dashboard-subscriptions').hidden = true;
    $('subscription-summary').hidden = true;
  }
}

async function loadPlanningFeatures() {
  try {
    const data = await api('/api/features');
    planningFeatures = { ...planningFeatures, ...data.features };
    applyPlanningVisibility();
    if (document.body.dataset.currentView === 'dashboard') loadPlanningDashboard();
  } catch (error) {
    // 기능 목록을 못 읽으면 모두 꺼진 화면(기존 화면)을 그대로 둡니다.
  }
}

document.querySelectorAll('[data-feature]').forEach((input) => {
  input.onchange = async () => {
    const feature = input.dataset.feature,
      enabled = input.checked;
    input.disabled = true;
    try {
      // 다른 저장으로 개인 자료 버전이 바뀌었을 수 있어 저장 직전에 최신 버전을 읽습니다.
      const latest = await api('/api/features');
      await api('/api/features', {
        method: 'PUT',
        revision: latest._revision,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ feature, enabled }),
      });
      planningFeatures[feature] = enabled;
      applyPlanningVisibility();
      notify(`‘${planningFeatureNames[feature]}’ 기능을 ${enabled ? '켰' : '껐'}습니다.`);
    } catch (error) {
      input.checked = !enabled;
      notify(error.message, true);
    } finally {
      input.disabled = false;
    }
  };
});

/** 지출 관리 탭을 열 때 그 탭의 자료를 읽습니다(finance.js의 showFinanceTab이 호출). */
function loadPlanningTab(name) {
  if (name === 'budgets' && planningFeatures.budgets) loadBudgets();
  if (name === 'tax' && planningFeatures.tax_deduction) loadTax();
  if (name === 'recurring' && planningFeatures.subscriptions) loadSubscriptionSummary();
}

/** 대시보드에 켜진 기능의 요약을 그립니다(app.js의 화면 이동이 호출). */
function loadPlanningDashboard() {
  if (planningFeatures.budgets) loadBudgetDashboard();
  if (planningFeatures.tax_deduction) loadTaxDashboard();
  if (planningFeatures.subscriptions) loadSubscriptionDashboard();
}

function openPlanningTab(tab) {
  navigateTo('planning');
  showFinanceTab(tab);
}
document
  .querySelectorAll('[data-open-tab]')
  .forEach((button) => (button.onclick = () => openPlanningTab(button.dataset.openTab)));
$('open-budget-alert').onclick = () => openPlanningTab('budgets');

// 2. 분야별 예산
// ─────────────────────────────────────────────────────────────────
function budgetBar(item) {
  const width = Math.min(100, item.ratio * 100);
  return /* HTML */ `<div class="budget-row budget-${item.status}">
    <div class="budget-row-head">
      <span>${escapeHTML(item.name)}</span>
      <span class="pattern-amount"
        >${won(item.spent)} / ${won(item.budget)}원 · ${Math.round(item.ratio * 100)}%${
          budgetStatusText[item.status] ? ` · ${budgetStatusText[item.status]}` : ''
        }</span
      >
    </div>
    <span class="pattern-track"><span class="pattern-fill" style="width:${width}%"></span></span>
  </div>`;
}

async function loadBudgets() {
  const month = $('finance-month').value || localMonth();
  $('budget-state').textContent = '예산을 확인하는 중입니다.';
  try {
    budgetData = await api(`/api/budgets?month=${encodeURIComponent(month)}`);
    if (!budgetData.enabled) return;
    $('budget-state').textContent = budgetData.items.length
      ? `${budgetData.month} 기준입니다.`
      : '아직 정한 예산이 없습니다. 아래에서 분야별 월 예산을 입력하세요.';
    $('budget-list').innerHTML = budgetData.items.map(budgetBar).join('');
    const current = Object.fromEntries(budgetData.items.map((item) => [item.category, item.budget]));
    const rows = [{ key: '__total__', name: '전체' }, ...budgetData.categories];
    $('budget-inputs').innerHTML = rows
      .map((category) => {
        const used = budgetData.spent_by_category[category.key];
        return `<label>${escapeHTML(category.name)}<input data-budget="${escapeHTML(category.key)}"
          inputmode="numeric" value="${current[category.key] ? won(current[category.key]) : ''}"
          placeholder="${used ? `이번 달 ${won(used)}원 사용` : '예산 없음'}" /></label>`;
      })
      .join('');
  } catch (error) {
    $('budget-state').textContent = error.message;
  }
}

$('budget-form').onsubmit = async (event) => {
  event.preventDefault();
  $('budget-error').textContent = '';
  $('budget-save').disabled = true;
  try {
    const items = [...document.querySelectorAll('[data-budget]')].map((input) => ({
      category: input.dataset.budget,
      amount: parseWon(input.value),
    }));
    await api('/api/budgets', {
      method: 'PUT',
      revision: budgetData?._revision,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items }),
    });
    notify('예산을 저장했습니다.');
    await loadBudgets();
  } catch (error) {
    $('budget-error').textContent = error.message;
  } finally {
    $('budget-save').disabled = false;
  }
};

async function loadBudgetDashboard() {
  try {
    const data = await api('/api/budgets');
    if (!data.enabled) return;
    $('dashboard-budget').hidden = !data.items.length;
    $('dashboard-budget-list').innerHTML = data.items.slice(0, 6).map(budgetBar).join('');
    const over = data.items.filter((item) => item.status === 'over').length,
      warn = data.items.filter((item) => item.status === 'warn').length;
    $('open-budget-alert').hidden = !(over || warn) || $('demo').checked;
    $('budget-alert-summary').textContent = [
      over ? `예산 초과 ${over}개 분야` : '',
      warn ? `80% 이상 ${warn}개 분야` : '',
    ]
      .filter(Boolean)
      .join(' · ');
    updateDashboardAlerts();
  } catch (error) {
    $('dashboard-budget').hidden = true;
  }
}

// 3. 연말정산 카드 공제
// ─────────────────────────────────────────────────────────────────
const taxTypeNames = { credit: '신용', debit: '체크', mixed: '혼합' };

/** 지금 상황에서 어떤 카드를 쓰면 좋은지 한 줄로 안내합니다. */
function taxAdvice(data) {
  if (!data.salary_set) return '총급여를 입력하면 공제 문턱과 예상 공제액을 계산해요.';
  if (data.to_threshold > 0)
    return `공제 문턱까지 ${won(data.to_threshold)}원 남았어요. 문턱 전까지는 카드 종류와 상관없이 공제가 없으니 혜택 좋은 신용카드를 써도 괜찮아요.`;
  if (data.deduction < data.basic_limit)
    return `문턱을 넘었어요. 지금부터는 체크카드·현금영수증(30%)이 신용카드(15%)보다 공제가 2배예요.`;
  return '기본 한도를 채웠어요. 이제는 전통시장·대중교통·문화비 사용분만 추가 공제돼요.';
}

function taxSummary(data) {
  const kinds = Object.keys(data.kind_names);
  return /* HTML */ `<div class="pattern-highlights">
      ${stat('공제 대상 사용액', won(data.spent) + '원')}
      ${stat('공제 문턱 (총급여 25%)', won(data.threshold) + '원')}
      ${stat('예상 공제액', won(data.deduction) + '원')}
      ${stat('연말 예상 공제액', won(data.projection.deduction) + '원')}
    </div>
    <div class="pattern-track" aria-hidden="true">
      <span
        class="pattern-fill"
        style="width:${data.threshold ? Math.min(100, (data.spent / data.threshold) * 100) : 0}%"
      ></span>
    </div>
    <p>${escapeHTML(taxAdvice(data))}</p>
    <p class="help">
      종류별: ${kinds
        .filter((kind) => data.by_kind[kind])
        .map((kind) => `${escapeHTML(data.kind_names[kind])} ${won(data.by_kind[kind])}원`)
        .join(' · ') || '아직 없음'}
      · 기본 한도 ${won(data.basic_limit)}원${data.extra ? ` · 추가 한도 인정 ${won(data.extra)}원` : ''}
    </p>
    <p class="help">
      모아 자료가 있는 달: ${
        data.months.length ? `${data.months[0]} ~ ${data.months.at(-1)} (${data.months.length}개월)` : '없음'
      }. 연말 예상은 이 기간의 월평균(${won(data.monthly_average)}원)을 남은 ${data.months_left}개월에
      신용카드로 쓴다고 보고 계산했어요.
    </p>
    ${
      data.fallback_rules
        ? `<p class="notice">${data.year}년 규칙이 아직 없어 ${data.rules_year}년 규칙으로 계산했습니다.</p>`
        : ''
    }`;
}

function fillTaxForm(data) {
  const form = $('tax-form'),
    settings = data.settings;
  form.elements.salary.value = settings.salary ? won(settings.salary) : '';
  form.elements.children.value = settings.children;
  $('tax-issuers').innerHTML = data.issuers
    .map(
      (issuer) => `<label>${escapeHTML(issuer)}<select data-tax-issuer="${escapeHTML(issuer)}">
        ${Object.entries(taxTypeNames)
          .map(
            ([value, name]) =>
              `<option value="${value}" ${(settings.issuer_types[issuer] || 'credit') === value ? 'selected' : ''}>${name}</option>`,
          )
          .join('')}</select></label>`,
    )
    .join('');
  $('tax-adjustments').innerHTML = Object.entries(data.kind_names)
    .map(
      ([kind, name]) => `<label>${escapeHTML(name)}<input data-tax-adjustment="${kind}"
        inputmode="numeric" value="${settings.adjustments[kind] ? won(settings.adjustments[kind]) : ''}"
        placeholder="0" /></label>`,
    )
    .join('');
}

async function loadTax() {
  $('tax-state').textContent = '올해 카드 사용액을 계산하는 중입니다.';
  try {
    taxData = await api('/api/tax-deduction');
    if (!taxData.enabled) return;
    $('tax-state').textContent = `${taxData.year}년 사용분 기준 추정입니다.`;
    $('tax-result').innerHTML = taxSummary(taxData);
    fillTaxForm(taxData);
  } catch (error) {
    $('tax-state').textContent = error.message;
  }
}

$('tax-form').onsubmit = async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  $('tax-error').textContent = '';
  $('tax-save').disabled = true;
  try {
    const payload = {
      year: taxData.year,
      salary: parseWon(form.elements.salary.value),
      children: Number(form.elements.children.value || 0),
      issuer_types: Object.fromEntries(
        [...document.querySelectorAll('[data-tax-issuer]')].map((select) => [
          select.dataset.taxIssuer,
          select.value,
        ]),
      ),
      adjustments: Object.fromEntries(
        [...document.querySelectorAll('[data-tax-adjustment]')].map((input) => [
          input.dataset.taxAdjustment,
          parseWon(input.value),
        ]),
      ),
    };
    await api('/api/tax-deduction/settings', {
      method: 'PUT',
      revision: taxData?._revision,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    notify('연말정산 설정을 저장했습니다.');
    await loadTax();
  } catch (error) {
    $('tax-error').textContent = error.message;
  } finally {
    $('tax-save').disabled = false;
  }
};

async function loadTaxDashboard() {
  try {
    const data = await api('/api/tax-deduction');
    if (!data.enabled) return;
    $('dashboard-tax').hidden = false;
    $('dashboard-tax-body').innerHTML = data.salary_set
      ? /* HTML */ `<p class="card-performance-amount">
            예상 공제액 <strong>${won(data.deduction)}원</strong> · 연말 예상
            ${won(data.projection.deduction)}원
          </p>
          <div class="pattern-track" aria-hidden="true">
            <span
              class="pattern-fill"
              style="width:${Math.min(100, (data.spent / data.threshold) * 100)}%"
            ></span>
          </div>
          <p class="help">${escapeHTML(taxAdvice(data))}</p>`
      : `<p class="help">${escapeHTML(taxAdvice(data))}</p>`;
  } catch (error) {
    $('dashboard-tax').hidden = true;
  }
}

// 4. 구독·정기 결제
// ─────────────────────────────────────────────────────────────────
function subscriptionLine(item) {
  return `<p>${escapeHTML(item.merchant)} · ${won(item.amount)}원${
    item.change > 0 ? ` <span class="tag budget-over-tag">+${won(item.change)}원 인상</span>` : ''
  } <span class="help">${escapeHTML(item.category)} · ${escapeHTML(item.card)} · 다음 예정 ${escapeHTML(item.next_expected)}</span></p>`;
}

async function loadSubscriptionSummary() {
  try {
    const data = await api('/api/subscriptions');
    if (!data.enabled) return;
    $('subscription-summary').hidden = false;
    $('subscription-stats').innerHTML =
      stat('정기 결제', `${data.count}건`) +
      stat('한 달 합계', won(data.monthly) + '원') +
      stat('1년 환산', won(data.yearly) + '원') +
      stat('구독료 분야', won(data.subscription_monthly) + '원/월');
    $('subscription-list').innerHTML =
      data.items.map(subscriptionLine).join('') ||
      '<p class="help">아직 3개월 이상 반복된 결제가 없습니다.</p>';
  } catch (error) {
    $('subscription-summary').hidden = true;
  }
}

async function loadSubscriptionDashboard() {
  try {
    const data = await api('/api/subscriptions');
    if (!data.enabled) return;
    $('dashboard-subscriptions').hidden = false;
    $('dashboard-subscriptions-body').innerHTML = /* HTML */ `<p class="card-performance-amount">
        정기 결제 ${data.count}건 · 월 <strong>${won(data.monthly)}원</strong> · 연
        ${won(data.yearly)}원
      </p>
      ${
        data.upcoming.length
          ? '<p class="help">7일 안에 결제 예정</p>' + data.upcoming.map(subscriptionLine).join('')
          : '<p class="help">7일 안에 예정된 정기 결제가 없어요.</p>'
      }
      ${
        data.increased.length
          ? `<p class="help">최근 가격이 오른 결제 ${data.increased.length}건</p>`
          : ''
      }`;
  } catch (error) {
    $('dashboard-subscriptions').hidden = true;
  }
}

$('finance-month').addEventListener('change', () => {
  if (!document.querySelector('[data-finance-panel="budgets"]').hidden) loadBudgets();
});
// 첫 화면 이동은 이 파일보다 먼저 실행되므로 여기서 기능 목록을 읽고 대시보드 요약을 그립니다.
loadPlanningFeatures();
