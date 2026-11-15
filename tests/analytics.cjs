/**
 * 가상 자료로 브라우저 화면 또는 계산 결과를 검증합니다.
 * 실제 거래·암호는 사용하지 않습니다. 브라우저 테스트는 Chromium 샌드박스를 켜고 실행합니다.
 */

const assert = require('node:assert/strict');
const { summarize, monthOffset, comparePeriods, calendarMonth } = require('../static/analytics');
const rows = [
  { used_on: '2026-10-03', merchant: '가상 가맹점', card: '삼성', amount: 100 },
  { used_on: '2026-10-05', merchant: '가상 가맹점', card: '삼성', amount: 200 },
  { used_on: '2026-10-05', merchant: '다른 가맹점', card: '신한', amount: 300 },
  { used_on: '2026-10-06', merchant: '다른 가맹점', card: '신한', amount: -500 },
  { used_on: '2026-09-01', merchant: '다른 가맹점', card: '삼성', amount: 1000 },
  { used_on: '2026-08-01', merchant: '다른 가맹점', card: '삼성', amount: -100 },
];
const report = summarize(rows, '2026-10');
assert.equal(report.gross, 600);
assert.equal(report.average, 200);
assert.equal(report.activeDays, 2);
assert.equal(report.weekendShare, 17);
assert.equal(report.weekdays[0].amount, 500);
assert.equal(report.weekdays[5].amount, 100);
assert.deepEqual(
  report.cards.map((item) => item.share),
  [50, 50],
);
assert.equal(report.merchants.find((item) => item.name === '가상 가맹점').count, 2);
assert.equal(report.trend.at(-1).amount, 100);
assert.equal(report.trend.at(-1).count, 4);
assert.equal(report.trend.at(-2).amount, 1000);
assert.equal(report.trend.at(-3).amount, -100);
assert.equal(report.trend[0].count, 0);
assert.equal(report.trend[0].amount, 0);
assert.equal(monthOffset('2026-01', -1), '2025-12');
assert.equal(monthOffset('2026-12', 1), '2027-01');
assert.equal(summarize([], '2026-10').average, 0);
assert.equal(summarize([], '2026-10').weekendShare, 0);
const refundOnly = summarize(
  [{ used_on: '2026-10-01', amount: -20, merchant: 'fixture', card: '삼성' }],
  '2026-10',
);
assert.equal(refundOnly.gross, 0);
assert.equal(refundOnly.trend.at(-1).amount, -20);
assert.deepEqual(refundOnly.cards, []);
const offset = summarize(
  [
    { used_on: '2026-10-01', amount: 20, merchant: 'fixture', card: '삼성' },
    { used_on: '2026-10-01', amount: -20, merchant: 'fixture', card: '삼성' },
  ],
  '2026-10',
);
assert.equal(offset.trend.at(-1).amount, 0);
assert.equal(offset.trend.at(-1).count, 2);
const categories = summarize(
  rows.map((row, i) => ({ ...row, category: i < 2 ? '커피' : i < 4 ? '식비' : undefined })),
  '2026-10',
);
assert.deepEqual(
  categories.categories.map((item) => [item.name, item.amount, item.count, item.share]),
  [
    ['커피', 300, 2, 50],
    ['식비', 300, 1, 50],
  ],
);
assert.deepEqual(summarize([], '2026-10').categories, []);
assert.equal(report.categories[0].name, '미분류');
assert.equal(report.categories[0].amount, 600);
const wallets = summarize(
  [
    {
      used_on: '2026-10-01',
      card: '삼성',
      merchant: '네이버페이',
      actual_merchant: '가상약국',
      category: '의료',
      amount: 100,
    },
    {
      used_on: '2026-10-02',
      card: '삼성',
      merchant: '네이버페이',
      actual_merchant: '가상식당',
      category: '식비',
      amount: 200,
    },
  ],
  '2026-10',
);
assert.deepEqual(
  wallets.merchants.map((item) => item.name),
  ['가상식당', '가상약국'],
);

const comparisonRows = [
  { used_on: '2026-11-01', amount: 100 },
  { used_on: '2026-11-03', amount: -20 },
  { used_on: '2026-11-04', amount: 9999 },
  { used_on: '2026-10-01', amount: 50 },
  { used_on: '2026-10-03', amount: 50 },
  { used_on: '2026-10-30', amount: 5000 },
];
const comparison = comparePeriods(comparisonRows, '2026-11', '2026-11-03');
assert.equal(comparison.currentTotal, 80);
assert.equal(comparison.previousTotal, 100);
assert.deepEqual(comparison.currentSeries, [100, 100, 80]);
assert.equal(comparison.previousSeries.at(-1), 5100);
assert.equal(comparison.inProgress, true);
assert.equal(comparePeriods(comparisonRows, '2026-10', '2026-11-03').cutoff, 31);
assert.equal(comparePeriods(comparisonRows, '2026-12', '2026-11-03').future, true);
const leap = comparePeriods([{ used_on: '2024-02-29', amount: 30 }], '2024-03', '2024-03-31');
assert.equal(leap.previousCutoff, 29);
assert.equal(leap.previousTotal, 30);
assert.equal(comparePeriods([], '2026-11', '2026-11-03').hasPrevious, false);
assert.equal(
  summarize([{ used_on: '2026-11-01', card: '삼성', amount: -40, merchant: '가상' }], '2026-11')
    .netCards[0].amount,
  -40,
);

// 달력: 2026-11-01은 일요일이라 앞 빈칸이 없고, 2026-10-01은 목요일이라 4칸입니다.
const calendar = calendarMonth(
  [
    { used_on: '2026-11-02', amount: 10000 },
    { used_on: '2026-11-02', amount: 30000 },
    { used_on: '2026-11-03', amount: 5000 },
    { used_on: '2026-11-04', amount: -2000 },
    { used_on: '2026-10-31', amount: 99999 },
  ],
  '2026-11',
  '2026-11-05',
  '2026-10-20',
);
assert.equal(calendar.lead, 0);
assert.equal(calendarMonth([], '2026-10', '2026-11-05').lead, 4);
assert.equal(calendar.days.length, 30);
assert.deepEqual(
  calendar.days.slice(0, 5).map((item) => [item.amount, item.count, item.level, item.noSpend]),
  [
    [0, 0, 0, true],
    [40000, 2, 4, false],
    [5000, 1, 1, false],
    [-2000, 1, 0, false],
    [0, 0, 0, true],
  ],
);
assert.equal(calendar.days[5].future, true);
assert.equal(calendar.days[5].noSpend, false);
assert.equal(calendar.total, 43000);
assert.equal(calendar.count, 4);
assert.equal(calendar.spentDays, 3);
assert.equal(calendar.noSpendDays, 2);
assert.equal(calendar.dailyAverage, 8600);
assert.equal(calendar.top.date, '2026-11-02');
// 첫 기록 전 날짜는 자료가 없으므로 지출 없는 날로 세지 않습니다.
assert.equal(calendarMonth([], '2026-11', '2026-11-05', '2026-11-04').noSpendDays, 2);
assert.equal(calendarMonth([], '2026-11', '2026-11-05', '').noSpendDays, 0);
