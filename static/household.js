/**
 * 통합 화면은 서버가 집계한 숫자만 표시합니다. 상대방의 원본 거래를 받아 합산하지 않습니다.
 * 내 원장과 통합 보기는 같은 대시보드 안에서 전환하며 개인정보는 브라우저 저장소에 쓰지 않습니다.
 */
let householdSequence = 0;
$('household-month').value = localMonth();

/** 집계 표시에는 거래로 이동하는 버튼을 만들지 않습니다. */
function householdBars(rows) {
  const maximum = Math.max(1, ...rows.map((row) => Math.abs(row.amount)));
  return (
    rows
      .map(
        (row) =>
          /* HTML */ `<div class="household-row">
            <span>${escapeHTML(row.name || row.month)}</span
            ><strong>${row.count === 0 ? '미등록' : won(row.amount) + '원'}</strong>
            <div class="household-meter">
              <span style="width:${(Math.abs(row.amount) / maximum) * 100}%"></span>
            </div>
          </div>`,
      )
      .join('') || '<p class="help">표시할 양수 사용액이 없습니다.</p>'
  );
}

/** 늦게 도착한 이전 월의 응답으로 현재 조회 결과를 덮어쓰지 않습니다. */
async function loadHousehold() {
  const sequence = ++householdSequence;
  const month = $('month').value || localMonth();
  $('household-month').value = month;
  $('household-refresh').disabled = true;
  $('household-state').textContent = '통합 합계를 확인하고 있습니다.';
  try {
    const data = await api('/api/household?month=' + encodeURIComponent(month));
    if (sequence !== householdSequence) return;
    $('household-stats').innerHTML =
      stat('통합 순사용액', won(data.net) + '원') +
      stat('양수 사용액', won(data.gross) + '원') +
      stat('취소·환불', won(data.refunds) + '원');
    $('household-categories').innerHTML = householdBars(data.categories);
    $('household-cards').innerHTML = householdBars(data.cards);
    $('household-trend').innerHTML = householdBars(data.trend);
    $('household-state').textContent =
      `${month} · 통합 ${won(data.count)}건 · 개인 상세 없이 합계만 공유합니다.`;
  } catch (error) {
    if (sequence === householdSequence) $('household-state').textContent = error.message;
  } finally {
    if (sequence === householdSequence) $('household-refresh').disabled = false;
  }
}

document.querySelectorAll('[data-dashboard-scope]').forEach((button) => {
  button.onclick = () => {
    const household = button.dataset.dashboardScope === 'household';
    $('personal-dashboard').hidden = household;
    $('household-dashboard').hidden = !household;
    document.querySelectorAll('[data-dashboard-scope]').forEach((item) => {
      const active = item === button;
      item.classList.toggle('active', active);
      item.setAttribute('aria-pressed', String(active));
    });
    if (household) loadHousehold();
    else if (typeof loadSmsDashboard === 'function') loadSmsDashboard();
  };
});
$('household-refresh').onclick = loadHousehold;
$('household-month').onchange = loadHousehold;
