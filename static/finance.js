/**
 * 지출 관리 화면: 할부·중복 후보·고정 지출의 조회와 버튼 동작을 연결합니다.
 * 실제 계산은 finance.py가 담당합니다. 편집을 시작한 화면 버전을 보관해 동시 수정을 확인합니다.
 */

let financeSnapshot = null,
  financeSequence = 0,
  editingPlan = null,
  editingPlanRevision = null;
$('finance-month').value = localMonth();
$('plan-first-month').value = localMonth();
/** 할부 편집 입력과 편집 시작 버전을 초기화합니다. */
function resetPlan() {
  editingPlan = null;
  editingPlanRevision = null;
  $('installment-form').reset();
  $('plan-first-month').value = $('finance-month').value || localMonth();
  $('plan-save').textContent = '할부 저장';
  renderPlanCards();
}
/** 서버 저장에 필요한 할부 필드만 골라 요청 객체를 만듭니다. */
function planPayload(plan) {
  return Object.fromEntries(
    ['name', 'card', 'principal', 'months', 'first_month', 'paid_count'].map((key) => [
      key,
      plan[key],
    ]),
  );
}
function stat(label, value) {
  return `<div><span>${label}</span><strong>${value}</strong></div>`;
}
/** 사용 중인 개인 카드사와 수정 중인 기존 할부의 카드사를 선택지로 만듭니다. */
function renderPlanCards() {
  if (!financeSnapshot?.cards) return;
  const selected =
    financeSnapshot.installments.plans.find((plan) => plan.id === editingPlan)?.card ||
    $('plan-card').value;
  $('plan-card').innerHTML =
    '<option value="">선택</option>' +
    financeSnapshot.cards
      .filter((row) => row.enabled || row.card === selected)
      .map(
        (row) =>
          `<option value="${escapeHTML(row.card)}">${escapeHTML(managementCardName(row))}${row.enabled ? '' : ' · 사용 해제'}</option>`,
      )
      .join('');
  $('plan-card').value = selected;
}

function planningCardName(card) {
  return managementCardName(financeSnapshot?.cards?.find((row) => row.card === card) || { card });
}
/** 지출 관리 자료를 받아 표시합니다. 늦게 도착한 이전 조회 결과는 무시합니다. */
async function loadFinance() {
  if ($('shared-management')?.hidden === false && typeof loadSharedManagement === 'function')
    return loadSharedManagement();
  const sequence = ++financeSequence;
  if (!$('finance-month').value) $('finance-month').value = localMonth();
  $('finance-refresh').disabled = true;
  $('finance-state').textContent = '지출 관리 자료를 확인하고 있습니다.';
  try {
    const data = await api('/api/finance?month=' + encodeURIComponent($('finance-month').value));
    if (sequence !== financeSequence) return;
    financeSnapshot = data;
    renderFinance();
    $('finance-state').textContent =
      '저장된 거래와 직접 등록한 할부 기준입니다. 예상액은 사용 내역에 합산하지 않습니다.';
  } catch (error) {
    if (sequence === financeSequence) $('finance-state').textContent = error.message;
  } finally {
    if (sequence === financeSequence) $('finance-refresh').disabled = false;
  }
}
/** 할부·중복·고정 지출 자료를 각 화면 영역에 표시합니다. */
function renderFinance() {
  renderPlanCards();
  const data = financeSnapshot,
    plans = data.installments;
  $('installment-stats').innerHTML =
    stat('남은 원금', won(plans.remaining_principal) + '원') +
    stat('기준 월 예상 원금', won(plans.this_month) + '원') +
    stat('진행 중 할부', plans.plans.filter((plan) => plan.remaining_count > 0).length + '개');
  $('installment-past').classList.toggle('hidden', !plans.past_unconfirmed);
  $('installment-past').textContent =
    `기준 월 이전 미확인 원금 ${won(plans.past_unconfirmed)}원. 이미 납부했다면 완료 회차를 수정해 주세요.`;
  $('installment-schedule').innerHTML = plans.schedule
    .map(
      (row) =>
        `<div><span>${escapeHTML(row.month)}</span><strong>${won(row.amount)}원</strong></div>`,
    )
    .join('');
  $('installment-list').innerHTML = plans.plans.length
    ? plans.plans
        .map(
          (plan) =>
            /* HTML */ `<article class="finance-item">
              <strong
                >${escapeHTML(plan.name)}
                <span class="tag">${escapeHTML(planningCardName(plan.card))}</span></strong
              >
              <p>
                ${plan.paid_count}/${plan.months}회 완료 · 남은 ${plan.remaining_count}회 ·
                ${won(plan.remaining_principal)}원
              </p>
              <p class="help">
                첫 납부 ${escapeHTML(plan.first_month)} · 종료 ${escapeHTML(plan.end_month)} ·
                ${plan.next_month ? '다음 미확인 ' + escapeHTML(plan.next_month) : '완료'}<br />회당
                원금 ${won(plan.regular_amount)}원 · 마지막 ${won(plan.final_amount)}원 · 수수료
                별도
              </p>
              <div class="finance-actions">
                <button
                  class="button"
                  data-plan-paid="${plan.id}"
                  ${!plan.remaining_count ? 'disabled' : ''}
                >
                  납부 1회 반영</button
                ><button class="button" data-plan-edit="${plan.id}">수정</button
                ><button class="delete" data-plan-delete="${plan.id}">삭제</button>
              </div>
            </article>`,
        )
        .join('')
    : '<p class="help">등록한 할부가 없습니다. 위의 ‘할부 등록 · 수정’을 펼쳐 추가하세요.</p>';
  $('duplicate-list').innerHTML = data.duplicates.length
    ? data.duplicates
        .map(
          (group) =>
            /* HTML */ `<article class="finance-item">
              <strong>${escapeHTML(group.merchant)}</strong>
              <p>
                ${escapeHTML(planningCardName(group.card))} · ${escapeHTML(group.used_on)} ·
                ${won(group.amount)}원 · 후보 ${group.items.length}건
              </p>
              ${group.items
                .map(
                  (row) =>
                    /* HTML */ `<div class="duplicate-source">
                      <div>
                        <strong>${escapeHTML(row.filename)}</strong
                        ><small
                          >${row.actual_merchant
                            ? `실제 가맹점: ${escapeHTML(row.actual_merchant)}<br>`
                            : ''}등록
                          ${escapeHTML(koreaTime(row.imported_at))} (한국 시간) · 거래
                          #${row.id}</small
                        >
                      </div>
                      <button class="button" data-exclude="${row.id}">중복으로 제외</button>
                    </div>`,
                )
                .join('')}
            </article>`,
        )
        .join('')
    : '<p class="help">서로 다른 파일 사이에서 일치하는 중복 후보가 없습니다.</p>';
  $('excluded-list').innerHTML = data.excluded.length
    ? data.excluded
        .map(
          (row) =>
            /* HTML */ `<div class="duplicate-source">
              <div>
                <strong>${escapeHTML(row.merchant)}</strong
                ><small
                  >${escapeHTML(planningCardName(row.card))} · ${escapeHTML(row.used_on)} ·
                  ${won(row.amount)}원<br />${escapeHTML(row.filename)}</small
                >
              </div>
              <button class="button" data-restore="${row.id}">복원</button>
            </div>`,
        )
        .join('')
    : '<p class="help">제외한 내역이 없습니다.</p>';
  const recurring = data.recurring.filter((row) => row.status !== 'ignored');
  $('recurring-stats').innerHTML =
    stat('반복 결제 후보', recurring.length + '개') +
    stat('최근 결제액 합계', won(recurring.reduce((sum, row) => sum + row.amount, 0)) + '원') +
    stat('직전 월 대비 변동', recurring.filter((row) => row.change !== 0).length + '개');
  function recurringRow(row) {
    return /* HTML */ `<article class="finance-item">
      <strong
        >${escapeHTML(row.merchant)}
        <span class="tag"
          >${row.status === 'confirmed'
            ? '고정 지출로 확인'
            : row.status === 'ignored'
              ? '후보 제외'
              : '검토할 후보'}</span
        ></strong
      >
      <p>
        ${escapeHTML(planningCardName(row.card))} · ${row.occurrences}개월 연속 · 최근
        ${won(row.amount)}원
      </p>
      <p class="help">
        직전 ${won(row.previous_amount)}원 → 최근 ${won(row.amount)}원 ·
        ${row.change === 0
          ? '금액 동일'
          : `${won(Math.abs(row.change))}원 ${row.change > 0 ? '증가' : '감소'}`}<br />마지막
        ${escapeHTML(row.last_used_on)} · 다음 예상 ${escapeHTML(row.next_expected)} /
        ${won(row.amount)}원
      </p>
      <details class="guidance">
        <summary>월별 결제 확인</summary>
        ${row.history
          .map((item) => `<p class="help">${escapeHTML(item.used_on)} · ${won(item.amount)}원</p>`)
          .join('')}
      </details>
      <div class="finance-actions">
        ${row.status !== 'confirmed' && row.status !== 'ignored'
          ? `<button class="button" data-recurring-id="${row.transaction_id}" data-recurring-status="confirmed">고정 지출로 확인</button>`
          : ''}<button
          class="button"
          data-recurring-id="${row.transaction_id}"
          data-recurring-status="${row.status === 'ignored' ? 'reset' : 'ignored'}"
        >
          ${row.status === 'ignored' ? '후보로 복원' : '후보에서 제외'}</button
        >${row.status === 'confirmed'
          ? `<button class="button" data-recurring-id="${row.transaction_id}" data-recurring-status="reset">확인 취소</button>`
          : ''}
      </div>
    </article>`;
  }
  $('recurring-list').innerHTML = recurring.length
    ? recurring.map(recurringRow).join('')
    : '<p class="help">조건에 맞는 반복 결제 후보가 없습니다. 최소 3개월 내역이 필요합니다.</p>';
  $('recurring-ignored').innerHTML =
    data.recurring
      .filter((row) => row.status === 'ignored')
      .map(recurringRow)
      .join('') || '<p class="help">제외한 후보가 없습니다.</p>';
}
/** 지출 관리 안에서 할부·중복·고정 지출·카드값 중 선택한 영역만 보입니다. */
function showFinanceTab(name) {
  if ($('shared-management')?.hidden === false && typeof showManagementScope === 'function')
    showManagementScope('personal');
  if (name === 'billing' && typeof loadBilling === 'function') loadBilling();
  if (name === 'performance' && typeof loadCardPerformance === 'function') loadCardPerformance();
  if (typeof loadPlanningTab === 'function') loadPlanningTab(name);
  document
    .querySelectorAll('[data-finance-panel]')
    .forEach((panel) => (panel.hidden = panel.dataset.financePanel !== name));
  document.querySelectorAll('[data-finance-tab]').forEach((button) => {
    const active = button.dataset.financeTab === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}
$('finance-refresh').onclick = () => {
  loadFinance();
  if ($('shared-management')?.hidden !== false && typeof loadBilling === 'function') loadBilling();
};
$('finance-month').onchange = () => {
  loadFinance();
  if ($('shared-management')?.hidden !== false && typeof loadBillingForMonth === 'function')
    loadBillingForMonth();
};
$('plan-reset').onclick = resetPlan;
$('installment-form').onsubmit = async (event) => {
  event.preventDefault();
  $('plan-save').disabled = true;
  const payload = {
    name: $('plan-name').value,
    card: $('plan-card').value,
    principal: $('plan-principal').value,
    months: $('plan-months').value,
    first_month: $('plan-first-month').value,
    paid_count: $('plan-paid').value,
  };
  try {
    await api(editingPlan ? `/api/installments/${editingPlan}` : '/api/installments', {
      method: editingPlan ? 'PUT' : 'POST',
      revision: editingPlan ? editingPlanRevision : financeSnapshot?._revision,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    resetPlan();
    await loadFinance();
    notify('할부 일정을 저장했습니다.');
  } catch (error) {
    notify(error.message, true);
  } finally {
    $('plan-save').disabled = false;
  }
};
/** 제외된 거래를 집계에 복원하고 최신 거래 자료를 읽습니다. */
async function restoreLedgerTransaction(id, revision = financeSnapshot?._revision) {
  await api(`/api/transactions/${id}/exclusion`, {
    method: 'PUT',
    revision,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ excluded: false }),
  });
  await refresh();
  notify('거래를 집계에 복원했습니다.');
}
$('transactions').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-ledger-restore]');
  if (!button) return;
  button.disabled = true;
  try {
    await restoreLedgerTransaction(button.dataset.ledgerRestore, state._revision);
  } catch (error) {
    button.disabled = false;
    notify(error.message, true);
  }
});
$('planning').addEventListener('click', async (event) => {
  const tab = event.target.closest('[data-finance-tab]');
  if (tab) {
    showFinanceTab(tab.dataset.financeTab);
    return;
  }
  const button = event.target.closest(
    '[data-plan-edit],[data-plan-delete],[data-plan-paid],[data-exclude],[data-restore],[data-recurring-id]',
  );
  if (!button || !financeSnapshot) return;
  const id = Number(
      button.dataset.planEdit || button.dataset.planDelete || button.dataset.planPaid,
    ),
    plan = financeSnapshot.installments.plans.find((item) => item.id === id);
  if (button.dataset.planEdit) {
    editingPlanRevision = financeSnapshot._revision;
    editingPlan = id;
    renderPlanCards();
    for (const [key, field] of [
      ['name', 'plan-name'],
      ['card', 'plan-card'],
      ['principal', 'plan-principal'],
      ['months', 'plan-months'],
      ['first_month', 'plan-first-month'],
      ['paid_count', 'plan-paid'],
    ])
      $(field).value = plan[key];
    $('plan-save').textContent = '할부 수정 저장';
    $('installment-editor').open = true;
    $('installment-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
    return;
  }
  button.disabled = true;
  try {
    if (button.dataset.planDelete) {
      if (!confirm('등록한 할부 일정을 삭제할까요?')) return;
      await api(`/api/installments/${id}`, {
        method: 'DELETE',
        revision: financeSnapshot?._revision,
      });
      if (editingPlan === id) resetPlan();
      await loadFinance();
    } else if (button.dataset.planPaid) {
      await api(`/api/installments/${id}`, {
        method: 'PUT',
        revision: financeSnapshot?._revision,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...planPayload(plan), paid_count: plan.paid_count + 1 }),
      });
      await loadFinance();
    } else if (button.dataset.exclude) {
      await api(`/api/transactions/${button.dataset.exclude}/exclusion`, {
        method: 'PUT',
        revision: financeSnapshot?._revision,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ excluded: true }),
      });
      await refresh();
      notify('거래는 보관하고 소비 집계에서 제외했습니다.');
    } else if (button.dataset.restore) {
      await restoreLedgerTransaction(button.dataset.restore);
    } else if (button.dataset.recurringId) {
      await api(`/api/transactions/${button.dataset.recurringId}/recurring`, {
        method: 'PUT',
        revision: financeSnapshot?._revision,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          status:
            button.dataset.recurringStatus === 'reset' ? null : button.dataset.recurringStatus,
        }),
      });
      await loadFinance();
    }
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});
if (!$('planning').hidden) loadFinance();
