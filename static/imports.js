/**
 * 파일 추가 화면: 저장 기록, CSV·엑셀 미리보기와 열 지정, 보안 HTML·PDF 표 선택, 로그아웃을 담당합니다.
 * app.js·transactions.js 다음에 읽습니다. 이 파일 끝의 refresh() 호출이 첫 화면 자료를 불러옵니다.
 * 이메일에서 연 명세서를 저장하면 mail.js의 refreshMail()로 목록을 갱신합니다.
 */

// 6. 저장 기록과 CSV·엑셀 가져오기
// ─────────────────────────────────────────────────────────────────
/** 저장한 명세서 목록을 표시하고 삭제 버튼을 연결할 정보를 넣습니다. */
function renderImports() {
  $('history-more').classList.toggle('hidden', state.imports.length <= historyLimit);
  $('imports').innerHTML = state.imports.length
    ? state.imports
        .slice(0, historyLimit)
        .map(
          (i) =>
            /* HTML */ `<div class="history-item">
              <div>
                <strong>${escapeHTML(i.filename)}</strong
                ><small
                  >${escapeHTML(i.card)}${i.payment_kind === 'card' ? '카드' : ''} · ${i.count}건 ·
                  등록 ${escapeHTML(i.created)} UTC</small
                >
              </div>
              <button class="delete" data-id="${i.id}">삭제</button>
            </div>`,
        )
        .join('')
    : '<p class="help">아직 가져온 파일이 없습니다. 원본 파일은 보관하지 않으며 등록 기록과 선택한 열만 저장합니다.</p>';
}
$('history-more').onclick = () => {
  historyLimit += 10;
  renderImports();
};
for (const id of ['month', 'demo'])
  $(id).addEventListener('change', () => {
    if (!$('month').value) $('month').value = localMonth();
    render();
  });
for (const id of [
  'tx-period',
  'tx-month',
  'tx-start',
  'tx-end',
  'filter-card',
  'filter-category',
  'tx-visibility',
  'search',
])
  $(id).addEventListener(id === 'search' ? 'input' : 'change', () => {
    limit = 20;
    renderTransactions();
  });
for (const [id, delta] of [
  ['prev', -1],
  ['next', 1],
])
  $(id).onclick = () => {
    const d = new Date(`${$('month').value || localMonth()}-01T12:00:00`);
    d.setMonth(d.getMonth() + delta);
    $('month').value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    limit = 20;
    render();
  };
$('more').onclick = () => {
  limit += 20;
  renderTransactions();
};
$('auto-header').addEventListener('change', () => {
  $('header-row').disabled = $('auto-header').checked;
  preview = null;
  $('mapping').classList.add('hidden');
});
for (const id of ['file', 'header-row', 'upload-card', 'upload-year'])
  $(id).addEventListener('change', () => {
    preview = null;
    $('mapping').classList.add('hidden');
  });
/** 추출된 표의 어떤 열이 날짜·가맹점·금액인지 고를 수 있게 표시합니다. */
function showMapping() {
  if (preview.header) $('header-row').value = preview.header;
  $('import-end').value = preview.count;
  $('import-end').max = preview.count;
  const options = preview.columns
    .map((c, i) => `<option value="${i}">${i + 1}. ${escapeHTML(c || '(제목 없음)')}</option>`)
    .join('');
  for (const [name, pattern] of [
    ['date', /이용일|사용일|거래일|승인일|매출일|결제일|날짜|일자|date/i],
    ['merchant', /가맹점|이용처|사용처|상호|업체명|merchant/i],
    ['amount', /이용금액|사용금액|승인금액|금액|이용액|사용액|청구액|원화|amount/i],
  ]) {
    const el = $(`map-${name}`);
    el.innerHTML = '<option value="">열을 선택해 주세요</option>' + options;
    const guess = preview.columns.findIndex((c) =>
      pattern.test(c.replace(/[\s\u200b\ufeff]+/g, '')),
    );
    if (guess >= 0) el.value = guess;
  }
  const actualIndex = preview.columns.findIndex((c) =>
    /실제.*가맹점|하위.*가맹점|세부.*가맹점|판매자|판매처|상점명/.test(c.replace(/\s+/g, '')),
  );
  $('map-actual-merchant').innerHTML = '<option value="">없음</option>' + options;
  const occupied = ['date', 'merchant', 'amount'].map((key) => $(`map-${key}`).value);
  if (actualIndex >= 0 && !occupied.includes(String(actualIndex)))
    $('map-actual-merchant').value = String(actualIndex);
  $('preview-count').textContent =
    `총 ${preview.count}개 데이터 행${preview.header ? ` · 제목 ${preview.header}행부터 ${preview.header_rows}행` : ''}${preview.excluded_rows ? ` · 마지막 합계 ${preview.excluded_rows}행 제외` : ''} · 처음 5행 미리보기`;
  $('preview-head').innerHTML =
    '<tr><th>행</th>' + preview.columns.map((c) => `<th>${escapeHTML(c)}</th>`).join('') + '</tr>';
  $('preview-body').innerHTML = preview.rows
    .map(
      (r, n) =>
        '<tr><td>' +
        String(n + 1) +
        '</td>' +
        preview.columns.map((_, i) => `<td>${escapeHTML(r[i] || '')}</td>`).join('') +
        '</tr>',
    )
    .join('');
  $('mapping').classList.remove('hidden');
  document.body.dataset.reviewStep = 'mapping';
  navigateTo('review');
}
$('upload-form').onsubmit = async (event) => {
  event.preventDefault();
  clearNotice();
  $('preview-button').disabled = true;
  preview = null;
  $('mapping').classList.add('hidden');
  try {
    const form = new FormData();
    form.append('file', $('file').files[0]);
    form.append('header', $('header-row').value);
    form.append('auto_header', $('auto-header').checked ? '1' : '0');
    form.append('year', $('upload-year').value);
    preview = await api('/api/preview', { method: 'POST', body: form });
    showMapping();
    await refreshMail();
  } catch (error) {
    notify(error.message, true);
  } finally {
    $('preview-button').disabled = false;
  }
};
$('import-button').onclick = async () => {
  if (!preview) return;
  const values = ['date', 'merchant', 'amount'].map((k) => $(`map-${k}`).value);
  if (values.some((v) => v === ''))
    return notify('날짜·가맹점·금액 열을 모두 선택해 주세요.', true);
  $('import-button').disabled = true;
  try {
    const data = await api('/api/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        token: preview.token,
        card: preview.card || $('upload-card').value,
        date: values[0],
        merchant: values[1],
        amount: values[2],
        actual_merchant: $('map-actual-merchant').value,
        end: $('import-end').value,
      }),
    });
    $('demo').checked = false;
    await refresh();
    await refreshMail();
    $('mapping').classList.add('hidden');
    preview = null;
    $('file').value = '';
    if (secureResult && secureResult.tables.length > 1) {
      renderSecureTable();
      document.body.dataset.reviewStep = 'tables';
      navigateTo('review');
    } else {
      resetSecure();
      $('secure-file').value = '';
      navigateTo(
        data.duplicate_count
          ? 'planning'
          : reviewOrigin === 'mail-inbox'
            ? 'mail-inbox'
            : 'dashboard',
      );
    }
    if (data.duplicate_count && !secureResult) {
      if (typeof showFinanceTab === 'function') showFinanceTab('duplicates');
    }
    notify(
      `${data.count}건을 저장했습니다.${data.duplicate_count ? ` 중복 후보 ${data.duplicate_count}건을 지출 관리에서 검토해 주세요.` : ''}`,
    );
  } catch (error) {
    notify(error.message, true);
  } finally {
    $('import-button').disabled = false;
  }
};
$('imports').onclick = async (event) => {
  const button = event.target.closest('[data-id]');
  if (!button) return;
  if (
    !confirm(
      '이 파일에서 가져온 사용 내역을 모두 삭제할까요? 그 거래에 붙인 영수증 사진도 함께 지워집니다.',
    )
  )
    return;
  button.disabled = true;
  try {
    await api(`/api/imports/${button.dataset.id}`, { method: 'DELETE' });
    await refresh();
    await refreshMail();
    notify('선택한 파일의 내역을 삭제했습니다.');
  } catch (error) {
    notify(error.message, true);
    button.disabled = false;
  }
};
refresh().catch((error) => notify('데이터를 불러오지 못했습니다. ' + error.message, true));

let secureResult = null,
  structureSample = null;
// 7. 보안 HTML·PDF의 표 선택과 검토
// ─────────────────────────────────────────────────────────────────
/** 명세서 검토용 브라우저 상태를 초기화합니다. 저장된 거래를 지우는 함수는 아닙니다. */
function resetSecure() {
  secureResult = null;
  structureSample = null;
  $('structure-download').classList.add('hidden');
  preview = null;
  $('mapping').classList.add('hidden');
  $('secure-selection').classList.add('hidden');
}
$('structure-button').onclick = () => {
  if (!structureSample) return;
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(structureSample, null, 2)], { type: 'application/json' }),
  );
  const link = document.createElement('a');
  link.href = url;
  link.download = 'moa-fictional-structure.json';
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
};
$('secure-file').addEventListener('change', () => {
  reviewOrigin = 'upload';
  resetSecure();
});
$('file').addEventListener('change', () => {
  reviewOrigin = 'upload';
});
$('secure-year').addEventListener('input', resetSecure);
$('secure-online').addEventListener('change', resetSecure);
$('secure-card').addEventListener('change', () => {
  resetSecure();
  $('secure-password').required = $('secure-card').value !== '신한';
  $('secure-online').disabled = $('secure-card').value !== '삼성';
  if ($('secure-online').disabled) $('secure-online').checked = false;
});
$('secure-password').addEventListener('input', resetSecure);
/** 여러 표 중 선택한 표의 일부 행과 제목·마지막 행 입력값을 표시합니다. */
function renderSecureTable() {
  preview = null;
  $('mapping').classList.add('hidden');
  const table = secureResult.tables[Number($('secure-table').value)];
  $('secure-header').value = table.suggested_header;
  $('secure-end').value = table.count;
  $('secure-table-preview').innerHTML = table.rows
    .map(
      (row, i) =>
        '<tr><td>' +
        String(i + 1) +
        '</td>' +
        row.map((v) => '<td>' + escapeHTML(v) + '</td>').join('') +
        '</tr>',
    )
    .join('');
}
$('secure-table').addEventListener('change', renderSecureTable);
for (const id of ['secure-header', 'secure-end'])
  $(id).addEventListener('input', () => {
    preview = null;
    $('mapping').classList.add('hidden');
  });
/** 보안 명세서 판독 결과를 받으면 표 선택 단계로 전환합니다. */
function setSecureResult(result) {
  secureResult = result;
  $('secure-table').innerHTML = secureResult.tables
    .map((t, i) => `<option value="${i}">거래 후보 ${i + 1} · ${t.count}행</option>`)
    .join('');
  renderSecureTable();
  $('secure-selection').classList.remove('hidden');
  document.body.dataset.reviewStep = 'tables';
  navigateTo('review');
}
$('secure-form').onsubmit = async (event) => {
  event.preventDefault();
  clearNotice();
  resetSecure();
  const form = new FormData();
  form.append('file', $('secure-file').files[0]);
  form.append('password', $('secure-password').value);
  form.append('year', $('secure-year').value);
  form.append('card', $('secure-card').value);
  form.append('allow_online', $('secure-online').checked ? '1' : '0');
  $('secure-password').value = '';
  $('unlock-button').disabled = true;
  $('secure-file').disabled = true;
  $('secure-password').disabled = true;
  $('secure-year').disabled = true;
  $('secure-card').disabled = true;
  $('secure-online').disabled = true;
  $('unlock-progress').classList.remove('hidden');
  try {
    setSecureResult(await api('/api/secure-html', { method: 'POST', body: form }));
    await refreshMail();
    notify('거래 후보를 추출했습니다. 원문과 건수·금액을 대조한 뒤 저장해 주세요.');
  } catch (error) {
    if (error.structure?.fictional === true) {
      structureSample = error.structure;
      $('structure-download').classList.remove('hidden');
      navigateTo('secure-upload');
      $('structure-download').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    notify(error.message, true);
    await refreshMail();
  } finally {
    form.delete('password');
    $('secure-password').value = '';
    $('unlock-button').disabled = false;
    $('secure-file').disabled = false;
    $('secure-password').disabled = false;
    $('secure-year').disabled = false;
    $('secure-card').disabled = false;
    $('secure-online').disabled = $('secure-card').value !== '삼성';
    $('unlock-progress').classList.add('hidden');
  }
};
$('secure-use').onclick = async () => {
  if (!secureResult) return;
  $('secure-use').disabled = true;
  try {
    preview = await api('/api/secure-preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        token: secureResult.token,
        table: Number($('secure-table').value),
        header: Number($('secure-header').value),
        end: Number($('secure-end').value),
      }),
    });
    showMapping();
  } catch (error) {
    notify(error.message, true);
  } finally {
    $('secure-use').disabled = false;
  }
};
window.addEventListener('pagehide', () => {
  $('secure-password').value = '';
  secureResult = null;
  structureSample = null;
});

$('logout-button').onclick = async () => {
  try {
    await api('/api/logout', { method: 'POST' });
    location.replace('/login');
  } catch (error) {
    notify(error.message, true);
  }
};
