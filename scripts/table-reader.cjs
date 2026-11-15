/**
 * 명세서 페이지 안에서 실행되는 표 판독 함수입니다.
 * 셀의 일반 텍스트를 읽어 날짜·금액 후보를 찾습니다. 페이지의 스크립트나 원본 속성을 결과로 보내지 않습니다.
 */

// Runs inside the isolated document. Only plain cell text leaves the browser.
/** 명세서의 표를 탐색해 일반 셀 텍스트와 거래 후보만 반환합니다. */
function extractTables() {
  const compact = (value) => value.replace(/[\s\u200b\ufeff]+/g, '');
  const dateLabel = /이용일|사용일|거래일|승인일|매출일|결제일|날짜|일자/;
  const merchantLabel = /가맹점|이용처|사용처|상호|이용가맹점|업체명/;
  const amountLabel = /금액|원화|이용액|사용액|청구액/;
  const dateValue =
    /^(?:\d{4}[-./년]\s*\d{1,2}[-./월]\s*\d{1,2}|\d{8}|\d{2}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2}|\d{1,2}\s*[-./]\s*\d{1,2})(?:일|\s|$)/;
  const found = [];
  const source = [...document.querySelectorAll('table')];
  if (source.length > 400) return { tables: [], oversized: true, tableCount: source.length };
  for (const table of source) {
    if (table.rows.length > 20000)
      return { tables: [], oversized: true, tableCount: source.length };
    const grid = [];
    let overflow = false;
    for (let y = 0; y < table.rows.length; y++) {
      const row = table.rows[y];
      grid[y] ||= [];
      let x = 0;
      for (const cell of row.cells) {
        while (grid[y][x] !== undefined) x++;
        const width = Math.max(1, cell.colSpan),
          height = Math.max(1, cell.rowSpan);
        if (x + width > 100 || height > 20000 || y + height > 20000) {
          overflow = true;
          break;
        }
        // Exclude nested table text from layout wrappers, but keep the cell's own text.
        let text;
        if (cell.querySelector('table')) {
          const copy = cell.cloneNode(true);
          copy.querySelectorAll('table,script,style').forEach((n) => n.remove());
          text = copy.textContent.trim();
        } else text = cell.innerText.trim();
        if (text.length > 2000) {
          overflow = true;
          break;
        }
        for (let dy = 0; dy < height; dy++) {
          grid[y + dy] ||= [];
          for (let dx = 0; dx < width; dx++) grid[y + dy][x + dx] = text;
        }
        x += width;
      }
      if (overflow) break;
    }
    if (overflow) continue;
    const rows = grid.map((r) => Array.from({ length: r.length }, (_, i) => r[i] || ''));
    const width = Math.max(0, ...rows.map((r) => r.length));
    if (width < 3 || rows.length < 2) continue;
    let header = null;
    // Prefer one complete row; then support headers split over two or three rows.
    for (let count = 1; count <= 3 && !header; count++) {
      for (let start = 0; start < Math.min(30, rows.length - count); start++) {
        const columns = Array.from({ length: width }, (_, x) =>
          [
            ...new Set(
              rows
                .slice(start, start + count)
                .map((r) => r[x] || '')
                .filter(Boolean),
            ),
          ].join(' / '),
        );
        const labels = columns.map(compact);
        if (
          labels.some((c) => dateLabel.test(c)) &&
          labels.some((c) => merchantLabel.test(c)) &&
          labels.some((c) => amountLabel.test(c))
        ) {
          header = { start, count, columns };
          break;
        }
      }
    }
    if (header) {
      // Rowspans repeat the date/merchant labels on the second header line.
      while (header.count < 3 && header.start + header.count < rows.length) {
        const next = rows[header.start + header.count];
        if (
          next.some((v) => dateValue.test(v.trim())) ||
          !next
            .map(compact)
            .some((v) => dateLabel.test(v) || merchantLabel.test(v) || amountLabel.test(v))
        )
          break;
        header.count++;
        header.columns = Array.from({ length: width }, (_, x) =>
          [
            ...new Set(
              rows
                .slice(header.start, header.start + header.count)
                .map((r) => r[x] || '')
                .filter(Boolean),
            ),
          ].join(' / '),
        );
      }
      found.push([header.columns, ...rows.slice(header.start + header.count)]);
    } else if (
      rows.some(
        (r) =>
          r.some((v) => dateValue.test(v.trim())) &&
          r.some((v) => /^[\s₩(+-]*[\d,]+(?:원|\))?\s*$/.test(v)) &&
          r.filter(Boolean).length >= 3,
      )
    ) {
      // Unknown header names: expose the table for manual mapping without guessing fields.
      found.push([Array.from({ length: width }, (_, x) => `열 ${x + 1}`), ...rows]);
    }
  }
  const unique = [...new Map(found.map((t) => [JSON.stringify(t), t])).values()];
  return { tables: unique, oversized: false, tableCount: source.length };
}
module.exports = { extractTables };
