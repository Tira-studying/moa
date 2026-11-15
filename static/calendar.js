/**
 * 사용 내역의 달력 보기: 날짜별 순사용액(환불 차감)을 한 달 달력으로 보여 줍니다.
 * 계산은 analytics.js의 MoaAnalytics.calendarMonth, 거래 필터는 transactions.js의 renderTransactions가 맡습니다.
 * 이 파일은 그 결과를 그리고, 날짜·월 이동·보기 전환 버튼을 처리합니다. 서버 요청은 없습니다.
 */

const CALENDAR_MODE_KEY = 'moa.activityMode';
const WEEKDAY_NAMES = ['일', '월', '화', '수', '목', '금', '토'];

/** 좁은 달력 칸에 맞춘 금액: 1만원 이상은 '6.3만', '128만'처럼 줄입니다. 정확한 금액은 칸의 설명에 있습니다. */
function compactWon(value) {
  const size = Math.abs(value),
    sign = value < 0 ? '-' : '';
  if (size >= 1_000_000) return `${sign}${won(Math.round(size / 10_000))}만`;
  if (size >= 10_000) return `${sign}${Math.round(size / 1_000) / 10}만`;
  return sign + won(size);
}

/** renderTransactions가 부릅니다. rows는 이번 달 필터 결과이고, 목록 보기면 false입니다. */
function renderSpendingCalendar(rows) {
  $('spending-calendar').hidden = !rows;
  if (!rows) return;
  const month = $('tx-month').value,
    today = MoaAnalytics.todayKST(),
    firstRecord = state.transactions.reduce(
      (first, row) => (!first || row.used_on < first ? row.used_on : first),
      '',
    ),
    data = MoaAnalytics.calendarMonth(rows, month, today, firstRecord);
  if (calendarDay && !calendarDay.startsWith(month)) calendarDay = '';
  $('calendar-title').textContent = `${month.slice(0, 4)}년 ${Number(month.slice(5))}월`;
  $('calendar-next').disabled = month >= today.slice(0, 7);
  $('calendar-summary').innerHTML = data.count
    ? `<span>합계 <b>${won(data.total)}원</b></span><span>쓴 날 ${data.spentDays}일</span><span>무지출 ${data.noSpendDays}일</span><span>하루 평균 ${won(data.dailyAverage)}원</span>${
        data.top
          ? `<span>가장 많이 쓴 날 ${Number(data.top.date.slice(8))}일 ${won(data.top.amount)}원</span>`
          : ''
      }`
    : '<span>이 달에는 조건에 맞는 사용 내역이 없어요.</span>';
  const blanks = '<span class="calendar-blank"></span>'.repeat(data.lead);
  $('calendar-days').innerHTML =
    blanks +
    data.days
      .map((item) => {
        const label = `${Number(month.slice(5))}월 ${item.day}일 ${WEEKDAY_NAMES[item.weekday]}요일, ${
          item.count
            ? `${item.count}건 ${won(item.amount)}원`
            : item.noSpend
              ? '지출 없음'
              : '기록 없음'
        }`;
        const classes = [
          'calendar-day',
          `level-${item.level}`,
          item.weekday === 0 ? 'sun' : item.weekday === 6 ? 'sat' : '',
          item.date === today ? 'today' : '',
          item.date === calendarDay ? 'selected' : '',
          item.amount < 0 ? 'refund-day' : '',
          item.future ? 'future' : '',
        ].filter(Boolean);
        return /* HTML */ `<button
          type="button"
          class="${classes.join(' ')}"
          data-calendar-day="${item.date}"
          aria-pressed="${item.date === calendarDay}"
          aria-label="${label}"
          title="${label}"
          ${item.future ? 'disabled' : ''}
        >
          <span class="calendar-date">${item.day}</span>${
            item.count
              ? `<span class="calendar-amount">${compactWon(item.amount)}</span><small>${item.count}건</small>`
              : item.noSpend
                ? '<small class="calendar-no-spend">무지출</small>'
                : ''
          }
        </button>`;
      })
      .join('');
  $('calendar-clear-day').hidden = !calendarDay;
}

/** 목록·달력 전환. 고른 방식은 이 브라우저에만 기억합니다(저장 실패해도 화면은 동작). */
function setActivityMode(mode) {
  activityMode = mode === 'calendar' ? 'calendar' : 'list';
  calendarDay = '';
  document.querySelectorAll('[data-activity-mode]').forEach((button) => {
    const active = button.dataset.activityMode === activityMode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  try {
    localStorage.setItem(CALENDAR_MODE_KEY, activityMode);
  } catch {}
  limit = 20;
  renderTransactions();
}
document
  .querySelectorAll('[data-activity-mode]')
  .forEach((button) => (button.onclick = () => setActivityMode(button.dataset.activityMode)));
for (const [id, delta] of [
  ['calendar-prev', -1],
  ['calendar-next', 1],
])
  $(id).onclick = () => {
    $('tx-month').value = MoaAnalytics.monthOffset($('tx-month').value || localMonth(), delta);
    calendarDay = '';
    limit = 20;
    renderTransactions();
  };
$('calendar-days').addEventListener('click', (event) => {
  const button = event.target.closest('[data-calendar-day]');
  if (!button || button.disabled) return;
  // 같은 날짜를 다시 누르면 한 달 전체 목록으로 돌아갑니다.
  calendarDay = calendarDay === button.dataset.calendarDay ? '' : button.dataset.calendarDay;
  limit = 20;
  renderTransactions();
  if (calendarDay) $('tx-summary').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
});
$('calendar-clear-day').onclick = () => {
  calendarDay = '';
  limit = 20;
  renderTransactions();
};
try {
  if (localStorage.getItem(CALENDAR_MODE_KEY) === 'calendar') setActivityMode('calendar');
} catch {}
