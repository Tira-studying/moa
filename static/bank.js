/** 개인 은행 입출금 화면입니다. 인증정보는 받지 않고 조회 자료는 메모리에만 둡니다. */
let bankSequence = 0;
const bankLabels = {
  ready: '자동 수집 가능',
  needs_auth: '인증 확인 필요 · 수집 중단',
  error: '연결 확인 필요 · 수집 중단',
  quota: '오늘 요청 예산 대기',
};
let bankData = null;
let bankEditor = null;
let bankReviewSequence = 0;
let bankSelectionBusy = false;

/** 정리 상태는 서버가 판단하며 화면 필터로 합계나 원장을 바꾸지 않습니다. */
function renderBankTransactions() {
  if (!bankData) return;
  const filter = $('bank-review-filter').value;
  const rows = bankData.transactions.filter((row) =>
    filter === 'pending'
      ? row.needs_review
      : filter === 'selected'
        ? row.selected
        : filter === 'unselected'
          ? row.eligible && !row.selected
          : true,
  );
  $('bank-transactions').innerHTML =
    (bankData.truncated ? '<p class="help">최신 1,000건 안에서 표시합니다.</p>' : '') +
    (rows
      .map(
        (row) => `<article class="bank-transaction">
      <div><strong>${escapeHTML(row.description || '거래 내역')}</strong><p class="help">${escapeHTML(row.used_on)} ${escapeHTML(row.used_time.slice(0, 2) + ':' + row.used_time.slice(2, 4))} · 우리은행 계좌 ${bankData.links.findIndex((link) => link.id === row.link_id) + 1}</p></div>
      <div class="bank-amount"><strong>${row.incoming ? '입금 +' + won(row.incoming) + '원' : row.outgoing ? '출금 −' + won(row.outgoing) + '원' : '금액 변동 없음'}</strong><p class="help">잔액 ${won(row.balance)}원</p></div>
      ${row.eligible ? `<div class="bank-review-actions"><label class="bank-consumption"><input type="checkbox" data-bank-consumption="${row.id}" ${row.selected ? 'checked' : ''} ${bankSelectionBusy ? 'disabled' : ''} /><span>소비로 선택</span></label><span class="bank-decision ${row.reflected ? 'reflected' : row.needs_review ? 'pending' : ''}">${escapeHTML(row.label)}</span><button class="button" type="button" data-bank-review="${row.id}">${row.needs_review ? '중복 확인' : '분류·상세'}</button></div>` : ''}
      </article>`,
      )
      .join('') || '<p class="help">이 조건에 맞는 입출금이 없습니다.</p>');
}

/** 늦게 도착한 응답이 다른 월의 내역을 덮어쓰지 않게 요청 순서를 확인합니다. */
async function loadBank() {
  const sequence = ++bankSequence;
  if (!$('bank-month').value) $('bank-month').value = localMonth();
  try {
    const data = await api('/api/bank?month=' + encodeURIComponent($('bank-month').value));
    if (sequence !== bankSequence) return;
    bankData = data;
    if (
      !data.links.length ||
      !data.worker_running ||
      data.links.some((row) => !row.enabled || row.state !== 'ready')
    )
      $('bank-connection-details').open = true;
    $('bank-state').textContent = data.links.length
      ? (data.worker_running ? '수집 작업자 실행 중' : '수집 작업자 상태를 확인해 주세요.') +
        ' · 한국시간 00·08·14·20시 · 최근 30일 수집'
      : '아직 연결한 계좌가 없습니다. 카드·계좌 연결에서 본인이 인증해 주세요.';
    $('bank-request').disabled = !data.links.some((row) => row.enabled && row.state === 'ready');
    const summary = data.review_summary || { pending_count: 0, reflected_count: 0 };
    const allSummary = data.all_review_summary || summary;
    $('bank-review-summary').textContent =
      `${data.month} · 소비 선택 ${summary.selected_count || 0}건 · 합계 반영 ${summary.reflected_count}건 · 중복 확인 ${allSummary.pending_count}건`;
    $('bank-next-pending').hidden =
      !allSummary.pending_month || allSummary.pending_month === $('bank-month').value;
    $('bank-links').innerHTML = data.links
      .map(
        (row, i) => `<article class="finance-item">
      <strong>우리은행 · 계좌 ${i + 1}</strong>
      <p>${row.enabled ? escapeHTML(bankLabels[row.state] || '상태 확인 필요') : '수집 중지'} · 마지막 성공 ${row.last_success ? escapeHTML(new Date(row.last_success * 1000).toLocaleString('ko-KR')) : '없음'}</p>
      <p class="help">${escapeHTML(row.message)}</p>
      ${row.enabled ? `<button class="button" data-bank-pause="${escapeHTML(row.id)}">수집 중지</button>` : ''}
    </article>`,
      )
      .join('');
    renderBankTransactions();
  } catch (error) {
    if (sequence === bankSequence) $('bank-state').textContent = error.message;
  }
}
$('bank-month').onchange = loadBank;
$('bank-review-filter').onchange = renderBankTransactions;
$('bank-next-pending').onclick = () => {
  const month = bankData?.all_review_summary?.pending_month;
  if (!month) return;
  $('bank-month').value = month;
  $('bank-review-filter').value = 'pending';
  loadBank();
};
$('bank-reload').onclick = loadBank;
$('bank-request').onclick = async () => {
  $('bank-request').disabled = true;
  try {
    await api('/api/bank/refresh', { method: 'POST' });
    notify('우리은행 수집을 요청했습니다. 잠시 후 화면을 새로고침하세요.');
    await loadBank();
  } catch (error) {
    notify(error.message, true);
  }
};
$('bank-links').onclick = async (event) => {
  const button = event.target.closest('[data-bank-pause]');
  if (!button) return;
  button.disabled = true;
  try {
    await api('/api/bank/pause', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: button.dataset.bankPause }),
    });
    await loadBank();
  } catch (error) {
    notify(error.message, true);
    button.disabled = false;
  }
};

/** 소비 반영은 명시적으로 고르고, 카드 후보가 있으면 별개 지출 확인을 받습니다. */
function updateBankReviewForm() {
  const decision = $('bank-review-decision').value;
  const expense = decision === 'expense';
  const duplicate = decision === 'card_duplicate';
  const selection = bankEditor?.selection;
  const count = bankEditor?.candidateCount || 0;
  $('bank-review-title').textContent = selection ? '소비 반영 확인' : '계좌 분류·상세';
  $('bank-decision-label').hidden = Boolean(selection);
  $('bank-review-decision').disabled = Boolean(selection);
  $('bank-expense-fields').hidden = !expense || selection;
  for (const id of ['bank-expense-merchant', 'bank-expense-category', 'bank-expense-memo'])
    $(id).disabled = !expense || selection;
  $('bank-expense-merchant').required = expense && !selection;
  $('bank-expense-category').required = expense && !selection;
  $('bank-separate-label').hidden = !expense || !count;
  $('bank-confirm-separate').disabled = !expense || !count;
  $('bank-confirm-separate').required = expense && Boolean(count);
  document.querySelectorAll('#bank-candidate-list input').forEach((input) => {
    input.disabled = !duplicate;
    input.required = duplicate;
  });
  $('bank-review-save').disabled = duplicate && !count;
  $('bank-review-save').textContent = expense ? '소비에 반영' : '정리 저장';
  $('bank-review-effect').textContent = expense
    ? '확인한 출금만 소비에 반영합니다. 나중에 새 카드 중복 후보가 들어오면 계좌 쪽 반영을 잠시 보류합니다.'
    : duplicate && !count
      ? '현재 연결할 카드 후보가 없습니다. 최신 내역을 확인하거나 다른 성격을 선택해 주세요.'
      : decision === 'pending'
        ? '소비 합계에 넣지 않습니다. 별도의 정리 알림은 표시하지 않습니다.'
        : '소비 합계에 더하지 않습니다. 이전에 반영한 지출은 내역을 보존하고 집계만 취소합니다.';
}

/** 편집을 열 때의 버전을 따로 보관해 뒤에서 실행되는 목록 조회가 덮어쓰지 못하게 합니다. */
async function openBankReview(identifier, draft = null, options = {}) {
  const sequence = ++bankReviewSequence;
  try {
    const data = await api(`/api/bank/transactions/${identifier}/review`);
    if (sequence !== bankReviewSequence) return;
    if (options.expectedRevision !== undefined && data._revision !== options.expectedRevision) {
      await loadBank();
      return notify('계좌 화면이 변경됐습니다. 최신 목록에서 다시 선택해 주세요.', true);
    }
    if (!data.transaction.eligible)
      return notify('입금과 금액 변동 없는 기록은 소비로 반영하지 않습니다.');
    if (options.selection && options.autoSelect && !data.candidate_count) {
      await saveBankConsumption(identifier, true, data._revision, data.candidate_token);
      return;
    }
    bankEditor = {
      id: identifier,
      revision: data._revision,
      token: data.candidate_token,
      candidateCount: data.candidate_count,
      selection: Boolean(options.selection),
    };
    $('bank-review-original').textContent =
      `${data.transaction.used_on} · 출금 ${won(data.transaction.outgoing)}원 · ${data.transaction.description}`;
    $('bank-review-hint').textContent = options.selection
      ? '카드 내역과 겹칠 가능성이 있습니다. 같은 거래라면 닫고 선택하지 않은 상태로 두세요.'
      : data.suggestion;
    $('bank-review-decision').innerHTML =
      '<option value="">선택해 주세요</option>' +
      data.decisions
        .map((item) => `<option value="${item.key}">${escapeHTML(item.name)}</option>`)
        .join('');
    $('bank-review-decision').value = options.selection
      ? 'expense'
      : (draft?.decision ??
        (data.transaction.decision === 'pending' ? '' : data.transaction.decision));
    $('bank-expense-category').innerHTML =
      '<option value="">분야를 선택해 주세요</option>' +
      data.categories
        .map((item) => `<option value="${escapeHTML(item.key)}">${escapeHTML(item.name)}</option>`)
        .join('');
    $('bank-expense-category').value = draft?.category ?? data.category ?? '';
    $('bank-expense-merchant').value =
      draft?.merchant ?? (data.merchant || data.transaction.description || '계좌 결제');
    $('bank-expense-memo').value = draft?.memo ?? data.memo;
    $('bank-confirm-separate').checked = false;
    $('bank-card-candidates').hidden = !data.candidate_count;
    $('bank-candidate-summary').textContent =
      `같은 금액·앞뒤 3일의 후보 ${data.candidate_count}건입니다. 동일 거래로 자동 판단하지 않습니다.${data.candidate_count > 20 ? ' 가까운 후보 20건만 표시합니다.' : ''}`;
    $('bank-candidate-list').innerHTML = data.candidates
      .map(
        (item) =>
          `<label class="bank-candidate"><input type="radio" name="bank-match" value="${item.id}" /><span><strong>${escapeHTML(item.merchant)}</strong><small>${escapeHTML(item.used_on)} · ${escapeHTML(item.card)} · ${won(item.amount)}원</small></span></label>`,
      )
      .join('');
    const selected = draft?.matched ?? data.matched_transaction_id;
    document.querySelectorAll('#bank-candidate-list input').forEach((input) => {
      input.checked = Number(input.value) === selected;
    });
    $('bank-review-error').textContent = '';
    updateBankReviewForm();
    if (!$('bank-review-editor').open) $('bank-review-editor').showModal();
  } catch (error) {
    if (sequence === bankReviewSequence) {
      notify(error.message, true);
      if (options.selection) await loadBank();
    }
  }
}
/** 체크의 성공 여부는 서버 응답으로 확정합니다. 재선택해도 기존 분류·메모를 유지합니다. */
async function saveBankConsumption(identifier, included, revision, token = '', separate = false) {
  await api(`/api/bank/transactions/${identifier}/consumption`, {
    method: 'PUT',
    revision,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ included, candidate_token: token, confirm_separate: separate }),
  });
  await Promise.all([refresh(), loadBank()]);
  $('conflict-notice').hidden = true;
  notify(included ? '선택한 내역을 소비에 반영했습니다.' : '소비 반영을 해제했습니다.');
}
$('bank-transactions').onchange = async (event) => {
  const input = event.target.closest('[data-bank-consumption]');
  if (!input || !bankData) return;
  const row = bankData.transactions.find(
    (item) => item.id === Number(input.dataset.bankConsumption),
  );
  if (!row) return;
  const included = input.checked;
  input.checked = row.selected;
  if (bankSelectionBusy) return;
  const revision = bankData._revision;
  bankSelectionBusy = true;
  renderBankTransactions();
  try {
    if (included)
      await openBankReview(row.id, null, {
        selection: true,
        autoSelect: true,
        expectedRevision: revision,
      });
    else await saveBankConsumption(row.id, false, revision);
  } catch (error) {
    notify(error.message, true);
    await loadBank();
  } finally {
    bankSelectionBusy = false;
    renderBankTransactions();
  }
};
$('bank-transactions').onclick = (event) => {
  const button = event.target.closest('[data-bank-review]');
  if (button) openBankReview(Number(button.dataset.bankReview));
};
$('bank-review-decision').onchange = updateBankReviewForm;
$('bank-review-close').onclick = () => $('bank-review-editor').close();
$('bank-review-editor').onclose = () => {
  bankEditor = null;
  bankReviewSequence++;
};
$('bank-review-reload').onclick = () => {
  if (!bankEditor) return;
  const selected = document.querySelector('#bank-candidate-list input:checked');
  openBankReview(
    bankEditor.id,
    {
      decision: $('bank-review-decision').value,
      category: $('bank-expense-category').value,
      merchant: $('bank-expense-merchant').value,
      memo: $('bank-expense-memo').value,
      matched: selected ? Number(selected.value) : null,
    },
    { selection: bankEditor.selection },
  );
};
$('bank-review-form').onsubmit = async (event) => {
  event.preventDefault();
  const editor = bankEditor;
  if (!editor) return;
  const selected = document.querySelector('#bank-candidate-list input:checked');
  $('bank-review-save').disabled = true;
  try {
    if (editor.selection) {
      await saveBankConsumption(
        editor.id,
        true,
        editor.revision,
        editor.token,
        $('bank-confirm-separate').checked,
      );
      if (bankEditor === editor) $('bank-review-editor').close();
      return;
    }
    await api(`/api/bank/transactions/${editor.id}/review`, {
      method: 'PUT',
      revision: editor.revision,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        decision: $('bank-review-decision').value,
        merchant: $('bank-expense-merchant').value,
        category: $('bank-expense-category').value,
        memo: $('bank-expense-memo').value,
        candidate_token: editor.token,
        confirm_separate: $('bank-confirm-separate').checked,
        matched_transaction_id: selected ? Number(selected.value) : null,
      }),
    });
    if (bankEditor === editor) $('bank-review-editor').close();
    await Promise.all([refresh(), loadBank()]);
    $('conflict-notice').hidden = true;
    notify('계좌 출금 정리를 저장했습니다.');
  } catch (error) {
    if (bankEditor === editor) $('bank-review-error').textContent = error.message;
  } finally {
    if (bankEditor === editor) updateBankReviewForm();
  }
};
setInterval(() => {
  if (!document.hidden && !$('bank').hidden) loadBank();
}, 30000);
loadBank.ready = true;
if (!$('bank').hidden) loadBank();
