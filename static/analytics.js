/**
 * 소비 분석 계산: 이미 읽은 거래 배열을 카드·분야·가맹점·요일별로 묶습니다.
 * 서버 통신과 DOM 변경이 없는 계산 모듈이며, 브라우저와 Node 테스트에서 함께 사용합니다.
 */

(function (root) {
  function monthOffset(month, offset) {
    const [year, value] = month.split('-').map(Number),
      d = new Date(Date.UTC(year, value - 1 + offset, 1));
    return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
  }
  function todayKST() {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Seoul',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(new Date());
    const value = (name) => parts.find((part) => part.type === name).value;
    return `${value('year')}-${value('month')}-${value('day')}`;
  }
  function monthDays(month) {
    const [year, number] = month.split('-').map(Number);
    return new Date(Date.UTC(year, number, 0)).getUTCDate();
  }
  /** 진행 중인 월만 오늘까지 비교하고, 전월 날짜는 실제 월말에서 멈춥니다. */
  function comparePeriods(rows, month, today = todayKST()) {
    const currentMonth = today.slice(0, 7),
      previous = monthOffset(month, -1);
    const days = monthDays(month),
      previousDays = monthDays(previous);
    const cutoff = month === currentMonth ? Number(today.slice(8)) : days;
    const previousCutoff = month === currentMonth ? Math.min(cutoff, previousDays) : previousDays;
    const current = rows.filter(
      (row) => row.used_on.startsWith(month) && Number(row.used_on.slice(8)) <= cutoff,
    );
    const prior = rows.filter((row) => row.used_on.startsWith(previous));
    const samePeriod = prior.filter((row) => Number(row.used_on.slice(8)) <= previousCutoff);
    const total = (items) => items.reduce((sum, row) => sum + row.amount, 0);
    const cumulative = (items, length) =>
      Array.from({ length }, (_, index) =>
        total(items.filter((row) => Number(row.used_on.slice(8)) <= index + 1)),
      );
    return {
      current,
      cutoff,
      days,
      previous,
      previousDays,
      previousCutoff,
      inProgress: month === currentMonth,
      future: month > currentMonth,
      currentTotal: total(current),
      previousTotal: total(samePeriod),
      hasPrevious: prior.length > 0,
      currentSeries: cumulative(current, cutoff),
      previousSeries: cumulative(prior, previousDays),
    };
  }
  function summarize(rows, month) {
    const current = rows.filter((row) => row.used_on.startsWith(month)),
      payments = current.filter((row) => row.amount > 0);
    const gross = payments.reduce((sum, row) => sum + row.amount, 0),
      byCard = new Map(),
      byCategory = new Map(),
      byMerchant = new Map(),
      weekdays = Array(7).fill(0);
    for (const row of payments) {
      byCard.set(row.card, (byCard.get(row.card) || 0) + row.amount);
      const name = row.category || '미분류',
        category = byCategory.get(name) || { name, amount: 0, count: 0 };
      category.amount += row.amount;
      category.count++;
      byCategory.set(name, category);
      const store = row.actual_merchant || row.merchant;
      const merchant = byMerchant.get(store) || { name: store, amount: 0, count: 0 };
      merchant.amount += row.amount;
      merchant.count++;
      byMerchant.set(store, merchant);
      const weekday = new Date(row.used_on + 'T00:00:00Z').getUTCDay();
      weekdays[(weekday + 6) % 7] += row.amount;
    }
    return {
      gross,
      average: payments.length ? Math.round(gross / payments.length) : 0,
      activeDays: new Set(payments.map((row) => row.used_on)).size,
      weekendShare: gross ? Math.round(((weekdays[5] + weekdays[6]) / gross) * 100) : 0,
      cards: [...byCard]
        .map(([name, amount]) => ({
          name,
          amount,
          share: gross ? Math.round((amount / gross) * 100) : 0,
        }))
        .sort((a, b) => b.amount - a.amount),
      netCards: [...new Set(current.map((row) => row.card))]
        .map((name) => ({
          name,
          amount: current
            .filter((row) => row.card === name)
            .reduce((sum, row) => sum + row.amount, 0),
        }))
        .sort((a, b) => b.amount - a.amount),
      categories: [...byCategory.values()]
        .map((item) => ({ ...item, share: gross ? Math.round((item.amount / gross) * 100) : 0 }))
        .sort((a, b) => b.amount - a.amount),
      merchants: [...byMerchant.values()].sort((a, b) => b.amount - a.amount).slice(0, 5),
      weekdays: weekdays.map((amount, index) => ({
        name: ['월', '화', '수', '목', '금', '토', '일'][index],
        amount,
      })),
      trend: Array.from({ length: 6 }, (_, i) => {
        const key = monthOffset(month, i - 5),
          items = rows.filter((row) => row.used_on.startsWith(key));
        return {
          month: key,
          amount: items.reduce((sum, row) => sum + row.amount, 0),
          count: items.length,
        };
      }),
    };
  }
  /**
   * 달력 한 달치 칸: 1일 앞의 빈칸 수와 날짜별 순사용액(환불 차감)·건수·색 단계를 만듭니다.
   * 지출 없는 날은 오늘까지, 그리고 첫 기록(firstRecord) 이후 날짜만 셉니다. 기록 전 날짜는 자료가 없는 것이기 때문입니다.
   */
  function calendarMonth(rows, month, today = todayKST(), firstRecord = '') {
    const [year, number] = month.split('-').map(Number),
      lead = new Date(Date.UTC(year, number - 1, 1)).getUTCDay(),
      byDay = new Map();
    for (const row of rows) {
      if (!row.used_on.startsWith(month)) continue;
      const item = byDay.get(row.used_on) || { amount: 0, count: 0 };
      item.amount += row.amount;
      item.count += 1;
      byDay.set(row.used_on, item);
    }
    const days = Array.from({ length: monthDays(month) }, (_, i) => {
      const date = `${month}-${String(i + 1).padStart(2, '0')}`,
        item = byDay.get(date) || { amount: 0, count: 0 };
      return {
        date,
        day: i + 1,
        weekday: (lead + i) % 7,
        ...item,
        future: date > today,
        noSpend: !item.count && date <= today && Boolean(firstRecord) && date >= firstRecord,
      };
    });
    const max = Math.max(0, ...days.map((item) => item.amount));
    for (const item of days)
      item.level = item.amount > 0 && max ? Math.min(4, Math.ceil((item.amount / max) * 4)) : 0;
    const counted = days.filter((item) => !item.future),
      spentDays = days.filter((item) => item.count),
      total = days.reduce((sum, item) => sum + item.amount, 0),
      top = days.reduce((best, item) => (item.amount > (best?.amount || 0) ? item : best), null);
    return {
      month,
      lead,
      days,
      total,
      count: days.reduce((sum, item) => sum + item.count, 0),
      spentDays: spentDays.length,
      noSpendDays: days.filter((item) => item.noSpend).length,
      // 하루 평균은 진행 중인 달이면 오늘까지의 날짜 수로 나눕니다.
      dailyAverage: counted.length ? Math.round(total / counted.length) : 0,
      top,
    };
  }
  const api = { summarize, monthOffset, todayKST, comparePeriods, calendarMonth };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.MoaAnalytics = api;
})(typeof window !== 'undefined' ? window : globalThis);
