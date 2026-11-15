/**
 * 롯데 양식의 보조 판독기입니다. 첫 셀에 함께 있는 날짜·가맹점을 나누고 날짜 없는 합계를 구분합니다.
 */

// Lotte statement: date and merchant share the first cell; totals lack a date.
/** 롯데카드의 날짜·가맹점 결합 셀을 거래 후보로 해석합니다. */
function extractLotte() {
  const source = [...document.querySelectorAll('table')];
  if (source.length > 400) return { tables: [], oversized: true };
  const result = [];
  for (const table of source) {
    if (table.rows.length > 20000) return { tables: [], oversized: true };
    const records = [];
    let titles = null;
    for (const row of table.rows) {
      const cells = [...row.cells];
      if (cells.length < 2 || cells.some((c) => c.querySelector('table'))) continue;
      const values = cells.map((c) => c.innerText.trim());
      if (values.some((v) => v.length > 2000)) continue;
      const labels = values.map((v) => v.replace(/\s/g, ''));
      if (labels.some((v) => v.includes('이용내역')) && labels.some((v) => /금액|원금/.test(v))) {
        titles = values.slice(1);
        continue;
      }
      const match = values[0].match(
        /^((?:\d{4}[-./])?\d{1,2}[-./]\d{1,2})(?:일)?[\s\n]+([\s\S]+)$/,
      );
      if (!match) continue;
      const merchant = match[2].replace(/\s+/g, ' ').trim();
      // Keep all amount/payment columns. Never choose billed principal as usage.
      if (!merchant || !values.slice(1).some((v) => /^[\s₩(+\-]*\d[\d,]*(?:원|\))?\s*$/.test(v)))
        continue;
      records.push([match[1], merchant, ...values.slice(1)]);
    }
    if (!records.length) continue;
    const count = Math.max(...records.map((r) => r.length)) - 2;
    const columns = [
      '이용일자',
      '가맹점명',
      ...Array.from(
        { length: count },
        (_, i) => titles?.[i] || (i === 0 ? '이용금액' : `추가 열 ${i + 1}`),
      ),
    ];
    result.push([columns, ...records]);
  }
  return { tables: result, oversized: false };
}
module.exports = { extractLotte };
