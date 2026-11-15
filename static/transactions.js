/**
 * 사용 내역 화면: 거래 검색·필터, 거래별 분류·실제 가맹점·메모 수정을 담당합니다.
 * app.js의 $, api, state, notify, render를 사용하므로 index.html에서 app.js 바로 다음에 읽습니다.
 * render()가 renderTransactions()를 호출합니다.
 */

// 5. 사용 내역 검색과 거래별 분류
// ─────────────────────────────────────────────────────────────────
/** 기간·검색·분류·중복 제외 조건에 맞는 거래 목록을 그립니다.
 * 달력 보기(calendar.js)에서는 조회 기간 대신 달력의 월을 쓰고, 고른 날짜가 있으면 목록을 그날로 좁힙니다. */
function renderTransactions() {
  const calendar = activityMode === 'calendar';
  if (calendar && !$('tx-month').value) $('tx-month').value = localMonth();
  document.querySelector('.activity-period').hidden = calendar;
  const period = calendar ? 'month' : $('tx-period').value,
    month = $('tx-month').value,
    start = $('tx-start').value,
    end = $('tx-end').value;
  $('tx-month-label').hidden = period !== 'month';
  $('tx-range').hidden = period !== 'custom';
  const invalid =
    (period === 'custom' && start && end && start > end) || (period === 'month' && !month);
  $('tx-period-error').textContent = invalid
    ? period === 'month'
      ? '조회할 월을 선택해 주세요.'
      : '시작일은 종료일보다 늦을 수 없습니다.'
    : '';
  const query = $('search').value.trim().toLowerCase(),
    card = $('filter-card').value,
    category = $('filter-category').value,
    visibility = $('tx-visibility').value;
  const monthRows = invalid
    ? []
    : state.transactions
        .filter(
          (t) =>
            (visibility === 'all' ||
              (visibility === 'excluded' ? Boolean(t.excluded) : !t.excluded)) &&
            (period !== 'month' || !month || t.used_on.startsWith(month)) &&
            (period !== 'custom' ||
              ((!start || t.used_on >= start) && (!end || t.used_on <= end))) &&
            (!$('tx-review-only').checked || t.category_reviewed === false) &&
            (!card || t.card === card) &&
            (!category || (t.category || '미분류') === category) &&
            [t.merchant, t.actual_merchant || '', t.memo || '']
              .join(' ')
              .toLowerCase()
              .includes(query),
        )
        .sort((a, b) => b.used_on.localeCompare(a.used_on) || b.id - a.id);
  if (typeof renderSpendingCalendar === 'function') renderSpendingCalendar(calendar && monthRows);
  const filtered =
    calendar && calendarDay ? monthRows.filter((t) => t.used_on === calendarDay) : monthRows;
  $('result-count').textContent = `${filtered.length}건`;
  $('tx-summary').textContent =
    `${calendar && calendarDay ? calendarDay : period === 'all' ? '전체 기간' : period === 'month' ? month || '월 미선택' : `${start || '처음'} ~ ${end || '최근'}`} · ${filtered.length}건 · ${visibility === 'active' ? '집계액' : visibility === 'excluded' ? '제외액' : '전체 기록 합계'} ${won(filtered.reduce((sum, t) => sum + t.amount, 0))}원 · ${Math.min(filtered.length, limit)}건 표시`;
  categoryReviewVisibleIds = filtered
    .slice(0, limit)
    .filter((row) => !row.excluded && row.category_reviewed === false)
    .map((row) => row.id);
  $('confirm-category-reviews').disabled = !categoryReviewVisibleIds.length;
  $('confirm-category-reviews').hidden = !categoryReviewVisibleIds.length;
  $('confirm-category-reviews').textContent =
    `표시된 ${Math.min(categoryReviewVisibleIds.length, 500)}건 확인 완료`;
  const filterCount =
    Number(visibility !== 'active') + Number(Boolean(category)) + Number(Boolean(card));
  $('activity-filter-count').hidden = !filterCount;
  $('activity-filter-count').textContent = filterCount;
  $('transactions').innerHTML = filtered
    .slice(0, limit)
    .map(
      (t) =>
        /* HTML */ `<tr role="row">
          <td class="transaction-date" role="cell">
            <time datetime="${escapeHTML(t.used_on)}"
              >${escapeHTML(t.used_on)}${t.used_time ? ` ${escapeHTML(t.used_time)}` : ''}</time
            >
          </td>
          <td class="merchant-cell" role="cell">
            <span>${escapeHTML(t.actual_merchant || t.merchant)}</span>${
              t.actual_merchant ? `<small>원본: ${escapeHTML(t.merchant)}</small>` : ''
            }${
              t.memo
                ? `<small>${escapeHTML(t.memo.slice(0, 60))}${t.memo.length > 60 ? '…' : ''}</small>`
                : ''
            }${
              t.receipt_ids
                ? `<small class="receipt-mark">영수증 사진 ${t.receipt_ids.split(',').length}장</small>`
                : ''
            }${
              t.payment_kind === 'manual'
                ? `<button class="button detail-button" type="button" data-manual-edit="${t.id}">수정</button>`
                : `<button class="button detail-button" type="button" data-transaction-details="${t.id}">상세·분류</button>`
            }
          </td>
          <td class="transaction-category" role="cell">
            <label class="category-edit"
              ><span class="sr-only">${escapeHTML(t.merchant)} 분야</span
              ><select data-category-id="${t.id}" aria-label="${escapeHTML(t.merchant)} 분야">
                ${categoryNames()
                  .map(
                    (name) =>
                      `<option ${name === (t.category || '미분류') ? 'selected' : ''}>${escapeHTML(name)}</option>`,
                  )
                  .join('')}
                <option value="__auto__">직접 지정 해제 · 기본 분류</option></select
              ><small
                >${
                  t.category_source === 'transaction'
                    ? '이번 거래 직접 분류'
                    : t.payment_service
                      ? '간편결제 · 이번 거래만'
                      : t.category_source === 'manual'
                        ? '가맹점 공통 분류'
                        : t.category_source === 'rule'
                          ? '자동 분류'
                          : '분류 전'
                }</small
              ></label
            >${
              t.category_reviewed === false && !t.excluded
                ? '<span class="category-review-tag">분류 확인 필요</span>'
                : ''
            }
          </td>
          <td class="transaction-card" role="cell">
            <span class="tag"
              >${escapeHTML(t.card)}${
                t.card_last_four ? ` ·${escapeHTML(t.card_last_four)}` : ''
              }</span
            >
          </td>
          <td class="transaction-amount right ${t.amount < 0 ? 'refund' : ''}" role="cell">
            ${won(t.amount)}원${
              t.excluded
                ? `<small class="excluded-label">${t.excluded_reason === 'not_spending' ? '소비 제외' : '집계 제외'}</small><button class="button" type="button" data-ledger-restore="${t.id}">복원</button>`
                : ''
            }${t.shared_hidden ? '<small class="excluded-label">상대에게 숨김</small>' : ''}
          </td>
        </tr>`,
    )
    .join('');
  $('empty').classList.toggle('hidden', filtered.length > 0);
  $('empty').querySelector('h3').textContent = !state.transactions.length
    ? '저장된 사용 내역이 없어요'
    : calendar && calendarDay
      ? '이 날은 사용 내역이 없어요'
      : '검색 조건에 맞는 내역이 없어요';
  // 첫 내역서 안내(CSV 시작·샘플)는 저장된 내역이 하나도 없을 때만 보여 줍니다.
  for (const item of $('empty').querySelectorAll('p, a'))
    item.hidden = state.transactions.length > 0;
  $('more').classList.toggle('hidden', filtered.length <= limit);
}
/** 상세 조건은 휴대폰에서 접어 두되, 적용 개수를 표시하고 기존 필터 값은 유지합니다. */
$('activity-filter-toggle').onclick = () => {
  const expanded = $('activity').classList.toggle('activity-filters-open');
  $('activity-filter-toggle').setAttribute('aria-expanded', String(expanded));
};
$('activity-filters-reset').onclick = () => {
  $('tx-visibility').value = 'active';
  $('filter-category').value = '';
  $('filter-card').value = '';
  limit = 20;
  renderTransactions();
};
/** 대시보드에서 누른 조건으로 사용 내역 탭을 열고 필터를 맞춥니다. */
function openActivity({
  month = '',
  card = '',
  merchant = '',
  category = '',
  reviewOnly = false,
} = {}) {
  $('tx-review-only').checked = reviewOnly;
  $('tx-period').value = month ? 'month' : 'all';
  if (month) $('tx-month').value = month;
  $('tx-visibility').value = 'active';
  $('filter-card').value = card;
  $('search').value = merchant;
  $('filter-category').value = category;
  calendarDay = '';
  limit = 20;
  renderTransactions();
  navigateTo('activity');
}
document.querySelectorAll('[data-pattern-tab]').forEach(
  (button) =>
    (button.onclick = () => {
      patternTab = button.dataset.patternTab;
      document.querySelectorAll('[data-pattern-tab]').forEach((item) => {
        const active = item === button;
        item.classList.toggle('active', active);
        item.setAttribute('aria-pressed', String(active));
      });
      renderPatterns(
        $('demo').checked ? demoData() : state.transactions.filter((row) => !row.excluded),
        $('month').value,
      );
    }),
);
$('pattern-trend').onclick = (event) => {
  if ($('demo').checked)
    return notify('가상 예시입니다. 실제 사용 내역은 사용 내역 탭에서 확인하세요.');
  const button = event.target.closest('[data-pattern-month]');
  if (button) openActivity({ month: button.dataset.patternMonth });
};
$('pattern-distribution').onclick = (event) => {
  if ($('demo').checked)
    return notify('가상 예시입니다. 실제 사용 내역은 사용 내역 탭에서 확인하세요.');
  const button = event.target.closest(
    '[data-pattern-category],[data-pattern-card],[data-pattern-merchant]',
  );
  if (button)
    openActivity({
      month: $('month').value,
      card: button.dataset.patternCard || '',
      merchant: button.dataset.patternMerchant || '',
      category: button.dataset.patternCategory || '',
    });
};

$('review-categories').onclick = () => openActivity({ month: $('month').value });
$('transactions').addEventListener('change', async (event) => {
  const select = event.target.closest('[data-category-id]');
  if (!select) return;
  const item = state.transactions.find((row) => row.id === Number(select.dataset.categoryId));
  if (!item) return;
  const category = select.value === '__auto__' ? null : select.value;
  document.querySelectorAll('[data-category-id]').forEach((input) => (input.disabled = true));
  try {
    await api(`/api/transactions/${item.id}/category`, {
      method: 'PUT',
      revision: state._revision,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        category,
        scope: item.category_scope || (item.payment_service ? 'transaction' : 'merchant'),
      }),
    });
    await refresh();
    notify(
      item.category_scope === 'transaction' || item.payment_service
        ? '이번 거래의 분야를 변경했습니다.'
        : '같은 가맹점의 공통 분류를 변경했습니다.',
    );
  } catch (error) {
    renderTransactions();
    notify(error.message, true);
  }
});

let transactionEditRevision = null,
  transactionEditId = null,
  transactionCategoryDirty = false,
  transactionPaymentService = null;
/** 간편결제의 실제 가맹점 입력 여부에 따라 공통 분류 선택과 안내를 바꿉니다. */
function updateTransactionScopeHelp() {
  const actual = $('transaction-actual').value.trim(),
    option = $('transaction-scope').querySelector('[value="merchant"]');
  option.disabled = Boolean(transactionPaymentService && !actual);
  option.textContent = actual ? '동일 실제 가맹점 공통' : '동일 가맹점 공통';
  if (option.disabled && $('transaction-scope').value === 'merchant')
    $('transaction-scope').value = 'transaction';
  $('transaction-scope-help').textContent = transactionPaymentService
    ? `${transactionPaymentService}는 이번 거래만 분류하는 것이 기본입니다. 실제 가맹점을 입력하고 공통 적용을 선택하면 그 실제 가맹점에만 분류를 기억합니다.`
    : '거래별 직접 분류가 가맹점 공통 분류보다 우선합니다. 실제 가맹점·메모만 수정하면 기존 분류 설정은 유지합니다.';
}
let transactionEditFlags = {};
/** 바꾼 체크만 보내서, 열어 둔 사이 다른 화면의 변경을 덮어쓰지 않습니다. */
function transactionFlagChanges() {
  const changes = {};
  const notSpending = $('transaction-not-spending');
  if (
    !notSpending.disabled &&
    !$('transaction-not-spending-label').hidden &&
    notSpending.checked !== transactionEditFlags.not_spending
  )
    changes.not_spending = notSpending.checked;
  if ($('transaction-shared-hidden').checked !== transactionEditFlags.shared_hidden)
    changes.shared_hidden = $('transaction-shared-hidden').checked;
  return changes;
}
/** 거래 상세를 채우고 편집 시작 당시 버전을 보관합니다. 나중 조회로 이 버전을 덮어쓰지 않습니다. */
function openTransactionEditor(item) {
  transactionEditRevision = state._revision;
  transactionEditId = item.id;
  transactionCategoryDirty = false;
  transactionPaymentService = item.payment_service || null;
  $('transaction-original').textContent =
    `${item.used_on} · ${item.card} · ${won(item.amount)}원 · 원본 가맹점: ${item.merchant}`;
  $('transaction-actual').value = item.actual_merchant || '';
  $('transaction-memo').value = item.memo || '';
  $('transaction-scope').value =
    item.category_scope || (item.payment_service ? 'transaction' : 'merchant');
  $('transaction-category').innerHTML =
    '<option value="__auto__">직접 지정 해제 · 기본 분류</option>' +
    categoryNames()
      .map((name) => `<option>${escapeHTML(name)}</option>`)
      .join('');
  $('transaction-category').value = ['manual', 'transaction'].includes(item.category_source)
    ? item.category
    : '__auto__';
  updateTransactionScopeHelp();
  // 중복으로 제외한 거래는 중복 검토에서만 되돌리고, 계좌 지출은 계좌 입출금에서 정합니다.
  const duplicate = Boolean(item.excluded) && item.excluded_reason !== 'not_spending';
  $('transaction-not-spending-label').hidden = item.payment_kind === 'bank';
  $('transaction-not-spending').checked = Boolean(item.excluded);
  $('transaction-not-spending').disabled = duplicate;
  $('transaction-not-spending-help').textContent = duplicate
    ? '중복으로 집계에서 뺀 거래입니다. 되돌리려면 사용 내역의 복원 버튼을 쓰세요.'
    : '카드 대금·회사 경비·상품권 충전처럼 실제 소비가 아닌 거래는 모든 합계·예산에서 뺍니다. 제외 내역 보기에서 다시 복원할 수 있어요.';
  $('transaction-shared-hidden').checked = Boolean(item.shared_hidden);
  transactionEditFlags = {
    not_spending: $('transaction-not-spending').checked,
    shared_hidden: $('transaction-shared-hidden').checked,
  };
  $('transaction-edit-error').textContent = '';
  transactionReceipts.open(item);
  $('transaction-editor').showModal();
}
$('transactions').addEventListener('click', (event) => {
  const button = event.target.closest('[data-transaction-details]');
  if (button) {
    const item = state.transactions.find(
      (row) => row.id === Number(button.dataset.transactionDetails),
    );
    if (item) openTransactionEditor(item);
  }
});
$('transaction-actual').oninput = updateTransactionScopeHelp;
for (const id of ['transaction-scope', 'transaction-category'])
  $(id).onchange = () => {
    transactionCategoryDirty = true;
  };
$('transaction-edit-close').onclick = () => $('transaction-editor').close();
$('transaction-detail-form').onsubmit = async (event) => {
  event.preventDefault();
  if (transactionEditId === null) return;
  $('transaction-edit-save').disabled = true;
  $('transaction-edit-error').textContent = '';
  try {
    await api(`/api/transactions/${transactionEditId}/category`, {
      method: 'PUT',
      revision: transactionEditRevision,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        category:
          $('transaction-category').value === '__auto__' ? null : $('transaction-category').value,
        scope: $('transaction-scope').value,
        update_category: transactionCategoryDirty,
        actual_merchant: $('transaction-actual').value,
        memo: $('transaction-memo').value,
        ...transactionFlagChanges(),
      }),
    });
    $('transaction-editor').close();
    await refresh();
    notify('거래 상세와 분류 설정을 저장했습니다.');
  } catch (error) {
    $('transaction-edit-error').textContent = error.message;
  } finally {
    $('transaction-edit-save').disabled = false;
  }
};
