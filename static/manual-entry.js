/**
 * 직접 입력 지출: 현금·계좌이체처럼 명세서가 없는 지출을 추가·수정·삭제하는 창입니다.
 * 서버 검사·저장은 manual_entries.py(/api/transactions/manual*)가 맡고, 저장 뒤 refresh()로 모든 화면을 다시 그립니다.
 * 창을 여는 버튼은 [data-manual-open](새로 입력)과 사용 내역 행의 [data-manual-edit](수정)입니다.
 */

let manualEditId = null,
  manualRevision = null;

/** '12,000원'처럼 적어도 숫자만 읽습니다. 숫자가 없으면 NaN입니다. */
function parseWon(value) {
  const digits = String(value).replace(/[^\d]/g, '');
  return digits ? Number(digits) : NaN;
}
function updateManualReceipt() {
  // 현금영수증은 현금·계좌이체에만 붙습니다(manual_entries.py RECEIPT_METHODS).
  const allowed = ['cash', 'transfer'].includes($('manual-method').value);
  document.querySelector('.manual-receipt').hidden = !allowed;
  if (!allowed) $('manual-receipt').checked = false;
}
/** item이 없으면 새 입력입니다. 달력에서 날짜를 골라 둔 상태면 그 날짜로 시작합니다. */
function openManualEntry(item = null) {
  manualEditId = item ? item.id : null;
  manualRevision = state._revision;
  $('manual-entry-title').textContent = item ? '직접 입력 수정' : '지출 직접 입력';
  $('manual-category').innerHTML = categoryNames()
    .map((name) => `<option>${escapeHTML(name)}</option>`)
    .join('');
  $('manual-date').value =
    item?.used_on ||
    (activityMode === 'calendar' && calendarDay ? calendarDay : MoaAnalytics.todayKST());
  $('manual-amount').value = item ? won(item.amount) : '';
  $('manual-merchant').value = item?.merchant || '';
  $('manual-method').value = item?.manual_method || 'cash';
  $('manual-category').value = item?.category || '식비';
  if (!$('manual-category').value) $('manual-category').selectedIndex = 0;
  $('manual-memo').value = item?.memo || '';
  $('manual-receipt').checked = Boolean(item?.cash_receipt);
  updateManualReceipt();
  $('manual-entry-delete').hidden = !item;
  $('manual-entry-error').textContent = '';
  manualReceipts.open(item);
  $('manual-entry').showModal();
  (item ? $('manual-amount') : $('manual-merchant')).focus();
}
document.addEventListener('click', (event) => {
  if (event.target.closest('[data-manual-open]')) return openManualEntry();
  const edit = event.target.closest('[data-manual-edit]');
  if (!edit) return;
  const item = state.transactions.find((row) => row.id === Number(edit.dataset.manualEdit));
  if (item) openManualEntry(item);
});
$('manual-method').onchange = updateManualReceipt;
$('manual-amount').addEventListener('input', () => {
  const amount = parseWon($('manual-amount').value);
  $('manual-amount').value = Number.isNaN(amount) ? '' : won(amount);
});
$('manual-entry-close').onclick = () => $('manual-entry').close();
$('manual-entry-form').onsubmit = async (event) => {
  event.preventDefault();
  const amount = parseWon($('manual-amount').value);
  if (!(amount > 0)) {
    $('manual-entry-error').textContent = '금액을 입력해 주세요.';
    return;
  }
  $('manual-entry-save').disabled = true;
  $('manual-entry-error').textContent = '';
  try {
    const saved = await api(
      manualEditId ? `/api/transactions/manual/${manualEditId}` : '/api/transactions/manual',
      {
        method: manualEditId ? 'PUT' : 'POST',
        revision: manualRevision,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          used_on: $('manual-date').value,
          amount,
          merchant: $('manual-merchant').value,
          method: $('manual-method').value,
          category: $('manual-category').value,
          memo: $('manual-memo').value,
          cash_receipt: $('manual-receipt').checked,
        }),
      },
    );
    // 새 지출이면 저장 뒤에 기다리던 영수증 사진을 올립니다(receipts.js).
    let failedPhotos = 0;
    if (!manualEditId) {
      manualRevision = saved._revision;
      failedPhotos = await manualReceipts.uploadPending(saved.id);
    }
    $('manual-entry').close();
    await refresh();
    notify(
      (manualEditId ? '직접 입력한 지출을 고쳤습니다.' : `${won(amount)}원 지출을 추가했습니다.`) +
        (failedPhotos
          ? ` 영수증 사진 ${failedPhotos}장은 올리지 못했어요. 수정 창에서 다시 올려 주세요.`
          : ''),
    );
  } catch (error) {
    $('manual-entry-error').textContent = error.message;
  } finally {
    $('manual-entry-save').disabled = false;
  }
};
$('manual-entry-delete').onclick = async () => {
  if (
    manualEditId === null ||
    !confirm('이 지출을 삭제할까요? 붙인 영수증 사진도 함께 지워지며 되돌릴 수 없습니다.')
  )
    return;
  $('manual-entry-delete').disabled = true;
  try {
    await api(`/api/transactions/manual/${manualEditId}`, {
      method: 'DELETE',
      revision: manualRevision,
    });
    $('manual-entry').close();
    await refresh();
    notify('직접 입력한 지출을 삭제했습니다.');
  } catch (error) {
    $('manual-entry-error').textContent = error.message;
  } finally {
    $('manual-entry-delete').disabled = false;
  }
};
