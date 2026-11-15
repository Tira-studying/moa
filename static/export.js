/**
 * 사용 내역 엑셀 내보내기: 사용 내역 화면에 지금 보이는 조건(기간·달력 날짜·검색·필터)을
 * /api/export.xlsx(export_xlsx.py)에 보내 xlsx 파일을 받습니다.
 * 휴대폰처럼 파일 공유를 지원하는 브라우저에서는 [공유·메일]로 메일·메신저 앱에 바로 첨부합니다.
 * 파일은 서버에 남지 않고, 이 화면에서도 내려받은 뒤 바로 버립니다.
 */

const XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

/** renderTransactions와 같은 규칙으로 현재 화면 조건을 주소 조건으로 바꿉니다. */
function exportParams() {
  const params = new URLSearchParams();
  if (activityMode === 'calendar' && calendarDay) {
    params.set('period', 'day');
    params.set('start', calendarDay);
  } else if (activityMode === 'calendar') {
    params.set('period', 'month');
    params.set('month', $('tx-month').value || localMonth());
  } else {
    const period = $('tx-period').value;
    params.set('period', period);
    if (period === 'month') params.set('month', $('tx-month').value);
    if (period === 'custom') {
      params.set('start', $('tx-start').value);
      params.set('end', $('tx-end').value);
    }
  }
  params.set('visibility', $('tx-visibility').value);
  for (const [name, id] of [
    ['card', 'filter-card'],
    ['category', 'filter-category'],
    ['q', 'search'],
  ])
    if ($(id).value.trim()) params.set(name, $(id).value);
  if ($('tx-review-only').checked) params.set('review_only', '1');
  return params;
}

/** 서버에서 파일을 받아 { blob, name }을 돌려줍니다. 오류면 서버 안내 문구로 실패합니다. */
async function fetchExport() {
  const response = await fetch('/api/export.xlsx?' + exportParams(), {
    headers: {
      'X-Requested-With': 'CardDashboard',
      ...(/^\d+$/.test(document.body.dataset.userId || '')
        ? { 'X-Moa-User': document.body.dataset.userId }
        : {}),
    },
  });
  if (response.status === 401) {
    location.replace('/login');
    throw new Error('로그인이 필요합니다.');
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || '엑셀 파일을 만들지 못했습니다. 다시 시도해 주세요.');
  }
  const header = response.headers.get('Content-Disposition') || '',
    encoded = header.match(/filename\*=UTF-8''([^;]+)/);
  return {
    blob: await response.blob(),
    name: encoded ? decodeURIComponent(encoded[1]) : 'moa-export.xlsx',
  };
}

async function runExport(button, action) {
  button.disabled = true;
  try {
    await action(await fetchExport());
  } catch (error) {
    // 사용자가 공유 창을 닫은 경우는 오류로 알리지 않습니다.
    if (error.name !== 'AbortError') notify(error.message, true);
  } finally {
    button.disabled = false;
  }
}

$('export-download').onclick = () =>
  runExport($('export-download'), async ({ blob, name }) => {
    const url = URL.createObjectURL(blob),
      link = document.createElement('a');
    link.href = url;
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notify(`${name} 파일을 내려받았습니다.`);
  });

$('export-share').onclick = () =>
  runExport($('export-share'), async ({ blob, name }) => {
    await navigator.share({
      files: [new File([blob], name, { type: XLSX_TYPE })],
      title: name,
    });
  });

// 파일 공유를 지원하는 브라우저(주로 휴대폰)에서만 [공유·메일]을 보여 줍니다.
try {
  $('export-share').hidden = !navigator.canShare?.({
    files: [new File([''], 'moa.xlsx', { type: XLSX_TYPE })],
  });
} catch {}
