/**
 * 카드값·명세서 화면: 월별 청구액·납부 여부와 카드 설정을 표시하고 저장합니다.
 * 입력한 청구액만 합산하며, 빈 입력과 0원은 서로 다르게 서버에 전달합니다.
 */

let billingSequence = 0;
let billingSummarySequence = 0;
let billingRequestedMonth = null;
const billingPath = (month, card) =>
  `/api/billing/${encodeURIComponent(month)}/${encodeURIComponent(card)}`;
/** 대시보드 '입력 청구액' 칸에 현재 월의 입력 청구액 합계를 표시합니다. 명세서 미확인 알림은 두지 않습니다. */
async function loadBillingSummary() {
  const sequence = ++billingSummarySequence;
  const month = $('month').value || localMonth();
  try {
    const data = await api('/api/billing?month=' + encodeURIComponent(month));
    if (sequence !== billingSummarySequence || month !== $('month').value) return;
    const entered = data.cards.some((row) => row.enabled && row.amount != null);
    $('dashboard-bill-total').textContent = entered ? won(data.total) + '원' : '미입력';
    $('dashboard-bill-total').title = data.unknown_count
      ? '일부 카드사의 청구액이 미입력 상태입니다.'
      : '사용자가 입력한 청구액 합계입니다.';
  } catch (error) {
    if (sequence !== billingSummarySequence) return;
    $('dashboard-bill-total').textContent = '확인 필요';
    $('dashboard-bill-total').title = error.message;
  }
}
/** 월 입력에서 change가 두 번 발생해도 같은 자료를 다시 그려 입력을 지우지 않습니다. */
function loadBillingForMonth() {
  if (billingRequestedMonth !== ($('finance-month').value || localMonth())) return loadBilling();
}
/** 조회 월의 카드별 입력 화면을 만듭니다. 각 폼은 그 자료를 읽을 때의 버전을 보관합니다. */
async function loadBilling() {
  const sequence = ++billingSequence,
    month = $('finance-month').value || localMonth();
  if (billingRequestedMonth !== month) {
    // 새 월을 읽는 동안 이전 월의 폼을 수정하거나 저장하지 않게 합니다.
    $('billing-list').replaceChildren();
    $('billing-settings').replaceChildren();
  }
  billingRequestedMonth = month;
  $('billing-state').textContent = '납부 현황을 확인하고 있습니다.';
  try {
    const data = await api('/api/billing?month=' + encodeURIComponent(month));
    if (sequence !== billingSequence) return;
    $('billing-state').textContent = `${month} 납부 기준 · 미확인 카드사: ${
      data.cards
        .filter((row) => row.enabled && row.statement_status === 'missing')
        .map(managementCardName)
        .join(', ') || '없음'
    }`;
    $('billing-stats').innerHTML =
      stat('입력 청구액 합계', won(data.total) + '원') +
      stat('아직 납부하지 않은 금액', won(data.unpaid) + '원') +
      stat('청구액 미입력', data.unknown_count + '곳');
    $('billing-card-add').dataset.revision = data._revision ?? '';
    $('billing-list').innerHTML =
      data.cards
        .filter((row) => row.enabled)
        .map(
          (row) =>
            /* HTML */ `<form
              class="finance-item bill-form"
              data-card="${escapeHTML(row.card)}"
              data-month="${month}"
              data-revision="${data._revision ?? ''}"
            >
              <h3>
                ${escapeHTML(managementCardName(row))}
                <span class="tag"
                  >${row.statement_status === 'missing'
                    ? '명세서 미확인'
                    : row.statement_status === 'no_bill'
                      ? '청구 없음'
                      : row.paid
                        ? '납부 완료'
                        : '명세서 확인 · 납부 전'}</span
                >
              </h3>
              <div class="form-grid">
                <label
                  >명세서 상태<select name="statement_status">
                    <option value="missing" ${row.statement_status === 'missing' ? 'selected' : ''}>
                      미확인
                    </option>
                    <option
                      value="received"
                      ${row.statement_status === 'received' ? 'selected' : ''}
                    >
                      명세서 확인
                    </option>
                    <option value="no_bill" ${row.statement_status === 'no_bill' ? 'selected' : ''}>
                      이번 달 청구 없음
                    </option>
                  </select></label
                ><label
                  >최종 청구액 (원)<input
                    name="amount"
                    type="number"
                    min="0"
                    max="1000000000000"
                    step="1"
                    inputmode="numeric"
                    placeholder="미입력"
                    value="${row.amount ?? ''}" /></label
                ><label
                  >이번 달 납부 예정일<input
                    name="due_on"
                    type="date"
                    min="${month}-01"
                    max="${month}-${new Date(
                      Number(month.slice(0, 4)),
                      Number(month.slice(5)),
                      0,
                    ).getDate()}"
                    value="${row.due_override || ''}"
                  /><small
                    >${row.due_override
                      ? '월별 지정'
                      : row.due_on
                        ? '기본 결제일: ' + row.due_on
                        : '기본 결제일 미설정'}
                    · 비우면 기본값 사용</small
                  ></label
                >
              </div>
              <label class="billing-check"
                ><input name="paid" type="checkbox" ${row.paid ? 'checked' : ''} /> 실제 납부
                완료</label
              >
              <p class="help bill-error" role="alert"></p>
              <button class="button primary">이 달 기록 저장</button>
            </form>`,
        )
        .join('') || '<p class="help">아래 설정에서 사용하는 카드사를 선택하세요.</p>';
    $('billing-settings').innerHTML = data.cards
      .map(
        (row) =>
          /* HTML */ `<form
            class="finance-item bill-setting"
            data-revision="${data._revision ?? ''}"
            data-card="${escapeHTML(row.card)}"
          >
            <h3>${escapeHTML(row.card)}${row.custom ? ' · 직접 추가' : ''}</h3>
            <label
              >나의 표시 이름<input
                name="display_name"
                maxlength="40"
                value="${escapeHTML(row.display_name || '')}"
                placeholder="${escapeHTML(row.card)}"
            /></label>
            <label class="billing-check"
              ><input name="enabled" type="checkbox" ${row.enabled ? 'checked' : ''} /> 사용
              중</label
            ><label
              >기본 결제일 (1~31일, 선택)<input
                name="due_day"
                type="number"
                min="1"
                max="31"
                step="1"
                inputmode="numeric"
                value="${row.due_day ?? ''}"
            /></label>
            <p class="help bill-error" role="alert"></p>
            <button class="button">카드 설정 저장</button>
          </form>`,
      )
      .join('');
  } catch (error) {
    if (sequence === billingSequence) {
      billingRequestedMonth = null;
      $('billing-state').textContent = error.message;
    }
  }
}
$('dashboard-bill-open').onclick = () => {
  $('finance-month').value = $('month').value || localMonth();
  if (typeof showManagementScope === 'function') showManagementScope('personal', false);
  navigateTo('planning');
  showFinanceTab('billing');
};
$('planning').addEventListener('submit', async (event) => {
  const form = event.target;
  if (!form.matches('.bill-form,.bill-setting')) return;
  event.preventDefault();
  const button = form.querySelector('button'),
    errorBox = form.querySelector('.bill-error');
  button.disabled = true;
  errorBox.textContent = '';
  const fields = form.elements,
    setting = form.matches('.bill-setting');
  const payload = setting
    ? {
        enabled: fields.enabled.checked,
        due_day: fields.due_day.value || null,
        display_name: fields.display_name.value,
      }
    : {
        statement_status: fields.statement_status.value,
        amount: fields.amount.value || null,
        due_on: fields.due_on.value || null,
        paid: fields.paid.checked,
      };
  const path = setting
    ? '/api/billing/settings/' + encodeURIComponent(form.dataset.card)
    : billingPath(form.dataset.month, form.dataset.card);
  try {
    await api(path, {
      method: 'PUT',
      revision: form.dataset.revision || null,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    await loadBilling();
    await loadBillingSummary();
    if (setting) await loadFinance();
    notify('납부 현황을 저장했습니다.');
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
loadBillingSummary();

/** 새 카드사는 개인 지출 관리에만 추가하며 명세서 판독기·문자 카드사 목록을 변경하지 않습니다. */
$('billing-card-add').onsubmit = async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button');
  const errorBox = $('billing-card-add-error');
  button.disabled = true;
  errorBox.textContent = '';
  try {
    await api('/api/billing/cards', {
      method: 'POST',
      revision: form.dataset.revision || null,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        card: form.elements.card.value,
        display_name: form.elements.display_name.value,
        due_day: form.elements.due_day.value || null,
      }),
    });
    form.reset();
    await loadBilling();
    await loadBillingSummary();
    await loadFinance();
    notify('내 카드사를 추가했습니다. 청구액과 할부를 등록할 수 있습니다.');
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    button.disabled = false;
  }
};
