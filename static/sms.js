/**
 * 아이폰 단축어 연결과 개인 문자 임시내역을 관리합니다.
 * 수신 키는 이번 화면의 메모리에만 보관하고 브라우저 저장소에 쓰지 않습니다.
 * 화면이 보일 때만 15초마다 조회하며 실제 단축어 전송 성공을 대신 보장하지 않습니다.
 */
let smsSequence = 0;
let smsLoading = false;
let smsKeyTimer;
$('sms-month').value = localMonth();
$('sms-endpoint').value = location.origin + '/api/sms/receive';

/** 단축어로 복사한 키가 다른 화면에 오래 노출되지 않도록 표시를 비웁니다. */
function clearSmsKey() {
  clearTimeout(smsKeyTimer);
  $('sms-key-value').value = '';
  $('sms-key-result').hidden = true;
}

/** 서버 문자열은 모두 이스케이프하며 상태 변경 버튼에는 숫자 ID만 사용합니다. */
function smsItem(row) {
  const pending = row.status === 'pending';
  const identity = row.last_four ? '끝 ' + escapeHTML(row.last_four) : '번호 미표시';
  const product = row.product ? ' · 상품 ' + escapeHTML(row.product) : '';
  const label = { pending: '미대조', settled: '대조 완료', ignored: '집계 제외' }[row.status];
  const reviewButton = pending
    ? '<button class="button" data-sms-status="settled">명세서와 대조 완료</button>'
    : '<button class="button" data-sms-status="pending">미대조로 복원</button>';
  const buttons =
    reviewButton +
    (row.status !== 'ignored'
      ? '<button class="button" data-sms-status="ignored">문자 집계에서 제외</button>'
      : '');
  return `<article class="finance-item" data-sms-id="${row.id}">
    <div><strong>${escapeHTML(row.merchant)}</strong><span>${won(row.amount)}원 · ${row.kind === 'approval' ? '승인' : '취소'}</span></div>
    <p class="help">${escapeHTML(row.card)} · ${identity}${product} · ${escapeHTML(row.occurred_at.slice(0, 16).replace('T', ' '))} (한국, 연도 추정)<br>${escapeHTML(row.payment)} · ${label}</p>
    ${row.statement_candidates ? `<p class="help">명세서 대조 후보 ${row.statement_candidates}건 · 자동 합산·확정하지 않습니다.</p>` : ''}
    <div class="planning-tabs">${buttons}</div>
  </article>`;
}

/** 월 변경 뒤 늦게 도착한 응답은 버리고, 현재 조회의 결과만 반영합니다. */
async function loadSms() {
  const sequence = ++smsSequence;
  smsLoading = true;
  $('sms-refresh').disabled = true;
  const month = $('sms-month').value || localMonth();
  try {
    const data = await api('/api/sms?month=' + encodeURIComponent(month));
    if (sequence !== smsSequence) return;
    $('sms-key-state').textContent = data.key
      ? `수신 키가 설정되어 있습니다. 마지막 수신: ${data.key.last_received ? data.key.last_received + ' UTC' : '아직 없음'}`
      : '수신 키를 발급하고 아이폰 단축어를 연결해 주세요.';
    $('sms-key-revoke').disabled = !data.key;
    const spending = data.spending;
    $('sms-stats').innerHTML =
      stat('문자 승인액', won(spending.approvals) + '원') +
      stat('문자 취소액', won(spending.cancellations) + '원') +
      stat('문자 기준 순사용액', won(spending.net) + '원');
    const items = data.items.filter(
      (row) => $('sms-show-completed').checked || row.status !== 'ignored',
    );
    $('sms-list').innerHTML =
      items.map(smsItem).join('') || '<p class="help">표시할 문자 임시내역이 없습니다.</p>';
    $('sms-state').textContent =
      `${month} · 문자 소비 ${spending.count}건 · 미대조 ${data.pending_count}건 · 대조 완료 ${spending.settled_count}건 · 제외 ${spending.ignored_count}건 · ${new Date().toLocaleTimeString('ko-KR')} 확인 · 화면을 보는 동안 15초마다 갱신합니다.`;
  } catch (error) {
    if (sequence === smsSequence) $('sms-state').textContent = error.message;
  } finally {
    if (sequence === smsSequence) {
      smsLoading = false;
      $('sms-refresh').disabled = false;
    }
  }
}

/** 대시보드에는 명세서 집계와 분리한 이번 달 문자 기준 소비만 요약합니다. */
let smsDashboardSequence = 0;
async function loadSmsDashboard() {
  // 숨겨 둔 문자 요약을 위해 화면 진입·복귀·주기 갱신에서 API를 호출하지 않습니다.
  if ($('sms-dashboard').hidden) return;
  const sequence = ++smsDashboardSequence;
  const month = localMonth();
  try {
    const data = await api('/api/sms?month=' + encodeURIComponent(month));
    if (sequence !== smsDashboardSequence) return;
    $('sms-dashboard-summary').textContent =
      `${month} · 문자 기준 순사용액 ${won(data.spending.net)}원 · ${data.spending.count}건 · 명세서 대조 완료 ${data.spending.settled_count}건`;
  } catch (error) {
    if (sequence === smsDashboardSequence) $('sms-dashboard-summary').textContent = error.message;
  }
}

$('sms-key-create').onclick = async () => {
  const button = $('sms-key-create');
  button.disabled = true;
  clearSmsKey();
  try {
    const data = await api('/api/sms/key', { method: 'POST' });
    // 사용자가 응답을 기다리는 동안 다른 화면으로 이동한 경우 키를 다시 노출하지 않습니다.
    if (!$('sms').hidden && document.visibilityState === 'visible') {
      $('sms-key-value').value = data.key;
      $('sms-key-result').hidden = false;
      smsKeyTimer = setTimeout(clearSmsKey, 90000);
    }
    await loadSms();
    notify('새 키를 발급했습니다. 기존 단축어의 키도 교체해 주세요.');
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
};
$('sms-key-revoke').onclick = async () => {
  const button = $('sms-key-revoke');
  button.disabled = true;
  try {
    await api('/api/sms/key', { method: 'DELETE' });
    clearSmsKey();
    await loadSms();
    notify('문자 수신 키를 해제했습니다.');
  } catch (error) {
    notify(error.message, true);
    button.disabled = false;
  }
};
$('sms-key-hide').onclick = clearSmsKey;
$('sms-refresh').onclick = loadSms;
$('sms-month').onchange = loadSms;
$('sms-show-completed').onchange = loadSms;
$('sms-list').onclick = async (event) => {
  const button = event.target.closest('[data-sms-status]');
  if (!button) return;
  button.disabled = true;
  try {
    await api('/api/sms/events/' + button.closest('[data-sms-id]').dataset.smsId, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: button.dataset.smsStatus }),
    });
    await loadSms();
    if (button.dataset.smsStatus === 'settled')
      notify('명세서 대조 완료로 표시했습니다. 문자 소비 내역과 합계는 유지됩니다.');
  } catch (error) {
    notify(error.message, true);
    button.disabled = false;
  }
};
setInterval(() => {
  if (document.visibilityState !== 'visible') return;
  if (!$('sms').hidden && !smsLoading) loadSms();
  else if (!$('dashboard').hidden && !$('personal-dashboard').hidden) loadSmsDashboard();
}, 15000);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') clearSmsKey();
  else if (!$('sms').hidden) loadSms();
  else if (!$('dashboard').hidden && !$('personal-dashboard').hidden) loadSmsDashboard();
});
if (!$('sms').hidden) loadSms();

if (!$('dashboard').hidden && !$('personal-dashboard').hidden) loadSmsDashboard();
