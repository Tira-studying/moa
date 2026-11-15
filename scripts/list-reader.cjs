/**
 * 표 대신 카드·목록 형태로 표시된 거래를 읽는 보조 판독기입니다.
 * 날짜·가맹점·금액을 같은 거래 영역에서 찾고, 불명확한 후보를 완전한 명세서로 간주하지 않습니다.
 */

// Conservative fallback for statements rendered as cards/lists instead of tables.
/** 목록형 거래에서 날짜가 있는 묶음을 찾아 열 형태의 후보로 바꿉니다. */
function extractList() {
  const dateRE =
    /(?:\d{4}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2}|\d{4}년\s*\d{1,2}월\s*\d{1,2}일|\d{2}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2}|\d{1,2}\s*[-./]\s*\d{1,2}|\d{8})(?!\d)/g;
  const labelRE =
    /^(이용금액|사용금액|승인금액|청구금액|원금|할부금액|결제금액|금액|이번달원금|이번달청구금액|할부원금|청구원금|할부수수료|수수료|이자액|이자|이용원금|청구잔액|잔액)\s*[:：]?\s*$/;
  const visible = (n) => {
    const parent = n.parentElement;
    if (
      !parent ||
      parent.closest('script,style,noscript,input') ||
      !parent.getClientRects().length ||
      getComputedStyle(parent).visibility === 'hidden'
    )
      return false;
    const form = parent.closest('form');
    return (
      !form ||
      ![...form.querySelectorAll('input[type=password]')].some(
        (p) => p.getClientRects().length && getComputedStyle(p).visibility !== 'hidden',
      )
    );
  };
  const dates = (text) => {
    text = text.normalize('NFKC').replace(/[\u200b\ufeff]/g, '');
    const matches = [...text.matchAll(dateRE)];
    return matches
      .filter((m) => {
        if (matches.length > 1) return true;
        const prefix = text.slice(0, m.index).replace(/\s/g, '');
        return (
          !prefix || /^(이용일자?|사용일자?|승인일자?|거래일자?|매출일자?|날짜)[:：]?$/.test(prefix)
        );
      })
      .map((m) => m[0].replace(/\s*([./-])\s*/g, '$1'))
      .filter((v) => {
        const numbers = v.match(/\d+/g).map(Number);
        if (numbers.length === 1 && v.length === 8)
          numbers.splice(0, 1, Number(v.slice(0, 4)), Number(v.slice(4, 6)), Number(v.slice(6, 8)));
        const month = numbers.length === 3 ? numbers[1] : numbers[0],
          day = numbers.at(-1);
        return month >= 1 && month <= 12 && day >= 1 && day <= 31;
      });
  };
  const groupedCache = new WeakMap();
  function groupedDate(node) {
    const first = node.parentElement;
    if (groupedCache.has(first)) return groupedCache.get(first);
    let group = first,
      result = null;
    for (let depth = 0; group && depth < 4; depth++, group = group.parentElement) {
      const text = group.innerText || '';
      if (text.length > 160) break;
      const found = dates(text);
      const bare = text
        .normalize('NFKC')
        .replace(/[\s\u200b\ufeff]/g, '')
        .replace(/^(?:이용일자?|사용일자?|승인일자?|거래일자?|매출일자?|날짜)[:：]?/, '');
      if (found.length === 1) {
        const dateText = found[0].replace(/\s/g, '');
        const tail = bare.startsWith(dateText) ? bare.slice(dateText.length) : null;
        const metadata =
          tail !== null &&
          /(?:본인|가족|카드번호|국내|해외|일시불|할부)/.test(tail) &&
          /^(?:(?:본인|가족|카드번호|국내|해외|일시불|할부)|[0-9*●·.…_:/()\-])*$/.test(tail);
        if (tail === '' || metadata) {
          result = { node: group, text: found[0] };
          break;
        }
      }
    }
    groupedCache.set(first, result);
    return result;
  }
  function atoms(element) {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const out = [],
      groupedSeen = new Set();
    let n;
    while ((n = walker.nextNode())) {
      if (!visible(n)) continue;
      if ([...groupedSeen].some((seen) => seen.contains(n.parentElement))) continue;
      const group = groupedDate(n);
      if (group && element.contains(group.node)) {
        // Date labels, the date and card metadata may share nested wrappers.
        // Keep one encompassing date atom rather than counting child+parent.
        for (let i = out.length - 1; i >= 0; i--)
          if (group.node.contains(out[i].node)) out.splice(i, 1);
        for (const seen of groupedSeen) if (group.node.contains(seen)) groupedSeen.delete(seen);
        out.push(group);
        groupedSeen.add(group.node);
        continue;
      }
      const value = n.textContent.trim();
      if (!value) continue;
      if (value.length > 1000 || out.length >= 200) return null;
      out.push({ text: value, node: n.parentElement });
    }
    return out;
  }
  function parse(element) {
    const parts = atoms(element);
    if (!parts) return null;
    if (
      parts.some((p) =>
        /^(결제예정일|결제계좌|이번달결제금액|총청구금액)[:：]?$/.test(p.text.replace(/\s/g, '')),
      )
    )
      return null;
    const allDates = parts.flatMap((p) => dates(p.text));
    if (allDates.length !== 1) return null;
    const money = [],
      moneyNodes = [],
      others = [],
      otherNodes = [];
    let explicitMerchant = null,
      headingMerchant = null,
      partial = false,
      cancel = false;
    const merchantGroup = (node) => {
      const group = node.closest(
        'h1,h2,h3,h4,h5,h6,strong,[class*="merchant"],[class*="store"],[class*="shop"],[class*="name"]',
      );
      if (!group || !element.contains(group)) return null;
      const text = group.innerText.replace(/\s+/g, ' ').trim();
      if (!text || text.length > 250 || dates(text).length || /[\d,]+\s*원|₩\s*[\d,]+/.test(text))
        return null;
      return text;
    };
    for (let i = 0; i < parts.length; i++) {
      const { text, node } = parts[i];
      if (dates(text).length) continue;
      if (/^(부분취소|부분환불)$/.test(text)) {
        partial = true;
        continue;
      }
      if (/^(취소|승인취소|결제취소|환불|취소완료|환불완료)$/.test(text)) {
        cancel = true;
        continue;
      }
      const merchant = text.match(/^(?:가맹점(?:명)?|이용처|사용처|상호|업체명)\s*[:：]\s*(.+)$/);
      if (merchant) {
        explicitMerchant = merchant[1];
        continue;
      }
      if (/^(가맹점명?|이용처|사용처|상호|업체명)\s*[:：]?$/.test(text)) {
        if (parts[i + 1]) explicitMerchant = parts[++i].text;
        continue;
      }
      const match = text.match(
        /^(?:(이용금액|사용금액|승인금액|청구금액|원금|할부금액|결제금액|금액|이번달원금|이번달청구금액|할부원금|청구원금|할부수수료|수수료|이자액|이자|이용원금|청구잔액|잔액)\s*[:：]?\s*)?([₩+\-]?\s*(?:\d{1,3}(?:,\d{3})+|\d+))\s*(원)?$/,
      );
      const previous = i > 0 ? parts[i - 1].text : '';
      const next = i + 1 < parts.length ? parts[i + 1].text : '';
      const amountContext = node.closest('[class*="amount"],[class*="price"],[class*="money"]');
      const isMoney =
        match &&
        (match[1] ||
          match[3] ||
          match[2].includes(',') ||
          text.includes('₩') ||
          labelRE.test(previous) ||
          /^(원|KRW|₩)$/.test(next) ||
          (amountContext && element.contains(amountContext)));
      if (isMoney) {
        moneyNodes.push(node);
        money.push({
          label:
            match[1] || (labelRE.test(previous) ? previous.replace(/[\s:：]/g, '') : '이용금액'),
          value: match[2].replace(/[₩\s,]/g, ''),
        });
        continue;
      }
      if (
        /^\d{1,2}$/.test(text) ||
        labelRE.test(text) ||
        /^(?:일시불|할부(?:\s*\d+\s*(?:개월|회))?|\d+\s*(?:개월|회)|국내|해외|원|KRW|₩|본인|가족|정상|승인|상세보기|자세히|접기|펼치기|이용일자?|사용일자?|승인일자?|카드번호|신한카드|삼성카드|현대카드|KB국민카드|우리카드|롯데카드)\s*[:：]?$/.test(
          text,
        ) ||
        /^[*●\d -]{4,}$/.test(text)
      )
        continue;
      if (/^(?:본인|가족)(?:\s|[/:：(*●\d-])/.test(text)) continue;
      if (text.length <= 200) {
        others.push(text);
        otherNodes.push(node);
        const grouped = merchantGroup(node);
        if (grouped) headingMerchant = grouped;
      }
    }
    let combined = null;
    if (otherNodes.length) {
      let group = otherNodes[0];
      while (group && group !== element && !otherNodes.every((n) => group.contains(n)))
        group = group.parentElement;
      if (group && group !== element) {
        const grouped = group.innerText.replace(/\s+/g, ' ').trim();
        if (
          grouped.length <= 250 &&
          !dates(grouped).length &&
          !/[\d,]+\s*원|₩\s*[\d,]+/.test(grouped)
        )
          combined = grouped;
      }
    }
    // In Samsung's small nested tables, merchant and amount share a row,
    // while the date/card metadata occupy another row. Keep cell boundaries.
    const merchantCells = new Set();
    for (const node of otherNodes) {
      const cell = node.closest('td,th'),
        row = cell?.parentElement;
      if (
        !cell ||
        !element.contains(cell) ||
        row?.tagName !== 'TR' ||
        !moneyNodes.some((m) => row.contains(m) && !cell.contains(m))
      )
        continue;
      const text = cell.innerText.replace(/\s+/g, ' ').trim();
      if (text && text.length <= 250 && !dates(text).length && !/[\d,]+\s*원|₩\s*[\d,]+/.test(text))
        merchantCells.add(text);
    }
    const rowMerchant = merchantCells.size === 1 ? [...merchantCells][0] : null;
    const merchant =
      explicitMerchant ||
      rowMerchant ||
      headingMerchant ||
      combined ||
      (others.length === 1 ? others[0] : null);
    if (!merchant || !money.length) return null;
    const primary = moneyNodes
      .map((node, index) => ({ node, index }))
      .filter(({ node }) => {
        const heading = node.closest('a,button');
        return (
          heading &&
          element.contains(heading) &&
          heading.querySelector('[class*="store"],[class*="merchant"],[class*="name"]')
        );
      });
    let extraNote = '';
    if (primary.length === 1) {
      const primaryIndex = primary[0].index;
      money[primaryIndex].label = '화면표시금액';
      let extra = 0;
      for (let index = 0; index < money.length; index++)
        if (index !== primaryIndex && money[index].label === '이용금액')
          money[index].label = '추가표시금액' + (++extra > 1 ? ' ' + extra : '');
      if (extra)
        extraNote = '추가 표시 금액은 화면표시금액과 합치지 않았습니다. 원문과 대조하세요.';
    }
    const labels = money.map((m) => m.label);
    if (partial || new Set(labels).size !== labels.length)
      return {
        date: allDates[0],
        review: '금액이 여러 개이거나 부분취소여서 원문 확인이 필요합니다.',
      };
    for (const item of money) if (cancel) item.value = String(-Math.abs(Number(item.value)));
    return {
      date: allDates[0],
      merchant,
      money,
      note: [cancel ? '명시된 취소·환불을 음수로 반영' : '', extraNote].filter(Boolean).join(' '),
    };
  }
  const walker = document.createTreeWalker(
    document.body || document.documentElement,
    NodeFilter.SHOW_TEXT,
  );
  const seeds = [],
    seedGroups = new Set();
  let n,
    scanned = 0;
  while ((n = walker.nextNode())) {
    if (++scanned > 30000) return { tables: [], oversized: true };
    if (visible(n)) {
      const group = groupedDate(n);
      if (group) {
        if ([...seedGroups].some((seen) => seen.contains(group.node))) continue;
        for (let i = seeds.length - 1; i >= 0; i--)
          if (group.node.contains(seeds[i].parentElement)) {
            seedGroups.delete(seeds[i].parentElement);
            seeds.splice(i, 1);
          }
        seeds.push({ parentElement: group.node });
        seedGroups.add(group.node);
      } else if (dates(n.textContent.trim()).length === 1) seeds.push(n);
    }
  }
  const subtotalNodes = new Set();
  let expectedCount = 0;
  const counts = document.createTreeWalker(
    document.body || document.documentElement,
    NodeFilter.SHOW_TEXT,
  );
  while ((n = counts.nextNode())) {
    if (!visible(n) || !/^\s*(?:소계\s*)?(?:총\s*)?[\d,]+\s*(?:건)?\s*$/.test(n.textContent))
      continue;
    let container = n.parentElement;
    for (let depth = 0; container && depth < 4; depth++, container = container.parentElement) {
      const text = container.innerText.trim();
      if (text.length > 150) break;
      if (/소계/.test(text) && [...text.matchAll(/([\d,]+)\s*건/g)].length === 1) {
        subtotalNodes.add(container);
        break;
      }
    }
  }
  const minimalCounts = [...subtotalNodes].filter(
    (n) => ![...subtotalNodes].some((other) => other !== n && n.contains(other)),
  );
  for (const container of minimalCounts)
    expectedCount += Number(container.innerText.match(/([\d,]+)\s*건/)[1].replace(/,/g, ''));
  const used = new Set(),
    records = [];
  for (const seed of seeds) {
    let element = seed.parentElement,
      chosen = null,
      chosenElement = null;
    for (
      let depth = 0;
      element && element !== document.body && depth < 8;
      depth++, element = element.parentElement
    ) {
      if (used.has(element)) {
        chosen = null;
        break;
      }
      const parts = atoms(element);
      if (!parts || parts.flatMap((p) => dates(p.text)).length > 1) break;
      const record = parse(element);
      const semantic =
        /^(LI|ARTICLE|TR|BUTTON)$/.test(element.tagName) ||
        element.getAttribute('role') === 'listitem' ||
        /(?:^|[\s_-])(?:item|record|transaction)(?:$|[\s_-])/.test(element.className || '');
      if (record && !chosen) {
        chosen = record;
        chosenElement = element;
      }
      if (semantic && record) {
        chosen = record;
        chosenElement = element;
        break;
      }
    }
    if (chosen && chosenElement) {
      used.add(chosenElement);
      records.push(chosen);
    }
  }
  if (!records.length)
    return {
      tables: [],
      oversized: false,
      dateNodes: seeds.length,
      expectedCount: subtotalNodes.size ? expectedCount : null,
      readCount: records.length,
    };
  const labels = [...new Set(records.flatMap((r) => (r.money || []).map((m) => m.label)))];
  if (!labels.length)
    return {
      tables: [],
      oversized: false,
      dateNodes: seeds.length,
      expectedCount: subtotalNodes.size ? expectedCount : null,
      readCount: records.length,
    };
  const table = [['이용일자', '가맹점명', ...labels, '검토 안내']];
  for (const r of records)
    table.push([
      r.date,
      r.merchant || '원문 확인 필요',
      ...labels.map((label) => (r.money || []).find((m) => m.label === label)?.value || ''),
      r.review || r.note || '목록에서 추출한 거래 후보 · 원문과 대조해 주세요',
    ]);
  return {
    tables: [table],
    oversized: table.length > 20000,
    dateNodes: seeds.length,
    expectedCount: subtotalNodes.size ? expectedCount : null,
    readCount: records.length,
  };
}
module.exports = { extractList };
