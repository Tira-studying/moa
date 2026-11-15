/**
 * 이메일 명세서 화면: 목록 표시, 즉시 새로고침, 여러 건 일괄 검토, 1분마다 자동 갱신을 담당합니다.
 * app.js·imports.js의 공통 도구와 미리보기 흐름(showMapping, setSecureResult 등)을 사용하므로 그 다음에 읽습니다.
 */

// 8. 이메일 목록·즉시 새로고침·일괄검토
// ─────────────────────────────────────────────────────────────────
let mailAutoPassword = false,
  mailSamsungOnline = false,
  mailItems = [],
  mailBulkRunning = false,
  mailBulkStop = false,
  mailLimit = 10;
const mailSelected = new Set();
let mailCollecting = false,
  mailRefreshPending = false,
  mailRefreshTimer = null;
const mailStages = {
  received: '수신됨',
  needs_review: '검토 필요',
  ready: '열기 성공',
  saved: '저장됨',
};
const koreaTime = (value) =>
  value
    ? new Date(value.replace(' ', 'T') + 'Z').toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' })
    : '아직 수집 전';
/** 메일 필터와 펼쳐 본 개수에 맞는 첨부 목록을 표시합니다. */
function renderMailItems() {
  const filter = $('mail-filter').value,
    items = mailItems.filter((item) =>
      filter === 'pending' ? item.stage !== 'saved' : !filter || item.stage === filter,
    );
  $('mail-more').classList.toggle('hidden', items.length <= mailLimit);
  $('mail-items').innerHTML = items.length
    ? items
        .slice(0, mailLimit)
        .map(
          (item) =>
            /* HTML */ `<article class="history-item mail-item">
              <div>
                <label class="mail-select-label"
                  ><input
                    type="checkbox"
                    data-mail-select="${escapeHTML(item.id)}"
                    ${mailSelected.has(item.id) ? 'checked' : ''}
                    ${item.stage === 'saved' ? 'disabled' : ''}
                  />
                  선택</label
                ><span class="mail-badge ${escapeHTML(item.stage)}"
                  >${mailStages[item.stage] || '수신됨'}</span
                ><strong>${escapeHTML(item.filename)}</strong
                ><small
                  >수집 ${escapeHTML(koreaTime(item.created))} ·
                  ${Math.ceil(item.size / 1024)}KB</small
                >
                <p class="help">
                  ${
                    item.saved_tables
                      ? `저장 ${item.saved_tables}개 표 · 거래 ${item.saved_count}건 · ${escapeHTML(koreaTime(item.saved_at))}<br>저장된 표 기준입니다. 다른 표가 있다면 추가로 검토하세요.`
                      : item.auto_error
                        ? escapeHTML(item.auto_error)
                        : item.stage === 'ready'
                          ? `거래 후보를 읽었습니다${item.auto_count != null ? ` · 후보 ${item.auto_count}행` : ''}. 열과 범위를 확인한 뒤 저장하세요.`
                          : item.state === 'opened'
                            ? '첨부파일을 불러왔습니다. 미리보기에서 내역을 확인해 주세요.'
                            : '명세서가 수집되었습니다. 카드사를 확인하고 검토를 시작하세요.'
                  }
                </p>
                <label
                  >카드사 확인
                  <select data-mail-card="${escapeHTML(item.id)}">
                    <option value="">선택해 주세요</option>
                    ${cards
                      .map(
                        (card) =>
                          `<option ${card === item.card ? 'selected' : ''}>${card}</option>`,
                      )
                      .join('')}
                  </select></label
                >
              </div>
              <button
                class="button"
                data-mail-open="${escapeHTML(item.id)}"
                data-mail-name="${escapeHTML(item.filename)}"
              >
                ${item.stage === 'saved' ? '다시 검토' : '검토 시작'}
              </button>
            </article>`,
        )
        .join('')
    : '이 상태의 명세서가 없습니다.';
  updateMailSelection();
}
$('mail-filter').onchange = () => {
  mailLimit = 10;
  renderMailItems();
};
$('mail-more').onclick = () => {
  mailLimit += 10;
  renderMailItems();
};
/** 서버에 저장된 이메일 목록·수집 진행 상태를 읽습니다. Gmail 수집 요청과는 별개입니다. */
async function refreshMail() {
  if (mailBulkRunning) return;
  clearTimeout(mailRefreshTimer);
  const selections = new Map(
    [...$('mail-items').querySelectorAll('[data-mail-card]')].map((el) => [
      el.dataset.mailCard,
      el.value,
    ]),
  );
  $('mail-refresh').disabled = true;
  $('mail-bulk').disabled = true;
  try {
    const result = await api('/api/mail');
    if (!result.configured) {
      mailCollecting = false;
      $('mail-state').textContent = result.error || 'Gmail 연결 설정이 필요합니다.';
      mailItems = [];
      mailSelected.clear();
      $('mail-summary').textContent = '';
      $('mail-list-note').textContent = '';
      renderMailItems();
      return;
    }
    mailAutoPassword = result.auto_password === true;
    mailSamsungOnline = result.samsung_online === true;
    const checked = result.status?.checked
      ? new Date(result.status.checked.replace(' ', 'T') + 'Z').toLocaleString('ko-KR', {
          timeZone: 'Asia/Seoul',
        })
      : '아직 수집 전';
    const control = result.refresh,
      busy = control && (control.running || control.requested > control.completed);
    const offline = busy && control.unresponsive === true;
    mailCollecting = Boolean(busy && !offline);
    const connection = offline
      ? '메일 수집기가 응답하지 않습니다. NAS의 mail-poller 실행 상태를 확인해 주세요.'
      : mailCollecting
        ? `Gmail ${control.running ? '수집 중' : '수집 요청 대기 중'}${control.remaining ? ` · 확인할 메일 ${control.remaining}통` : ''}`
        : control?.error ||
          result.status?.error ||
          `${result.status?.checked ? '수집 완료 · ' : ''}${mailAutoPassword ? '공통 암호 설정됨 · ' : ''}${mailSamsungOnline ? '삼성 공식 연결 허용 · ' : ''}${result.folder} 라벨 · 자동 수집 5분 간격 · 마지막 확인 ${checked} (한국 시간)${control?.remaining ? ` · 남은 메일 ${control.remaining}통: 새로고침을 다시 누르면 이어서 수집합니다.` : ''}`;
    $('mail-state').textContent = [connection, result.password_error].filter(Boolean).join(' · ');
    if (mailCollecting) mailRefreshTimer = setTimeout(refreshMail, 1500);
    mailItems = result.items.map((item) => ({
      ...item,
      card: selections.get(item.id) ?? item.card,
    }));
    for (const id of mailSelected)
      if (!mailItems.some((item) => item.id === id && item.stage !== 'saved'))
        mailSelected.delete(id);
    $('mail-summary').innerHTML = Object.entries(mailStages)
      .map(
        ([stage, label]) =>
          `<div><span>${label}</span><strong>${result.summary?.[stage] || 0}<small>개</small></strong></div>`,
      )
      .join('');
    $('mail-list-note').textContent =
      `첨부파일 ${result.total ?? mailItems.length}개 · 최근 100개 · 저장됨은 기본 목록에서 숨김 · 한국 시간 기준`;
    renderMailItems();
  } catch (error) {
    mailCollecting = false;
    $('mail-state').textContent = error.message;
  } finally {
    $('mail-refresh').disabled = mailCollecting || mailRefreshPending;
    $('mail-refresh').textContent = mailCollecting ? 'Gmail 수집 중…' : '목록 새로고침';
    updateMailSelection();
  }
}
/** 체크한 명세서 수와 일괄검토 버튼 상태를 맞춥니다. */
function updateMailSelection() {
  const visible = [...$('mail-items').querySelectorAll('[data-mail-select]:not(:disabled)')];
  const checked = visible.filter((el) => el.checked).length;
  $('mail-select-all').checked = visible.length > 0 && checked === visible.length;
  $('mail-select-all').indeterminate = checked > 0 && checked < visible.length;
  $('mail-select-all').disabled = mailBulkRunning || !visible.length;
  $('mail-bulk').textContent = `선택한 명세서 일괄검토 (${mailSelected.size})`;
  $('mail-bulk').disabled = mailBulkRunning || $('mail-refresh').disabled || !mailSelected.size;
}
$('mail-items').addEventListener('change', (event) => {
  const card = event.target.closest('[data-mail-card]');
  if (card) {
    const item = mailItems.find((item) => item.id === card.dataset.mailCard);
    if (item) item.card = card.value;
    return;
  }
  const input = event.target.closest('[data-mail-select]');
  if (!input) return;
  if (input.checked) mailSelected.add(input.dataset.mailSelect);
  else mailSelected.delete(input.dataset.mailSelect);
  updateMailSelection();
});
$('mail-select-all').onchange = () => {
  for (const input of $('mail-items').querySelectorAll('[data-mail-select]:not(:disabled)')) {
    input.checked = $('mail-select-all').checked;
    if (input.checked) mailSelected.add(input.dataset.mailSelect);
    else mailSelected.delete(input.dataset.mailSelect);
  }
  updateMailSelection();
};
$('mail-bulk-stop').onclick = () => {
  mailBulkStop = true;
  $('mail-bulk-stop').disabled = true;
  $('mail-bulk-progress').textContent = '진행 중인 파일 확인이 끝나면 중단합니다.';
};
$('mail-bulk').onclick = async () => {
  if (mailBulkRunning) return;
  const choices = new Map(
    [...$('mail-items').querySelectorAll('[data-mail-card]')].map((el) => [
      el.dataset.mailCard,
      el.value,
    ]),
  );
  const queue = mailItems
    .filter((item) => mailSelected.has(item.id) && item.stage !== 'saved')
    .map((item) => ({ ...item, card: choices.get(item.id) ?? item.card }));
  if (!queue.length) return;
  if (queue.some((item) => !item.card))
    return notify('선택한 명세서의 카드사를 모두 확인해 주세요.', true);
  mailBulkRunning = true;
  mailBulkStop = false;
  $('mail-bulk-results').textContent = '';
  $('mail-bulk-stop').classList.remove('hidden');
  $('mail-bulk-stop').disabled = false;
  for (const input of $('mail-items').querySelectorAll('input,select,button'))
    input.disabled = true;
  $('mail-filter').disabled = true;
  $('mail-refresh').disabled = true;
  $('mail-more').disabled = true;
  updateMailSelection();
  let success = 0,
    failed = 0;
  try {
    for (const item of queue) {
      if (mailBulkStop) break;
      $('mail-bulk-progress').textContent =
        `${success + failed + 1}/${queue.length} 확인 중 · ${item.filename}`;
      const result = document.createElement('li');
      try {
        const data = await api(`/api/mail/${encodeURIComponent(item.id)}/check`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ card: item.card }),
        });
        success++;
        mailSelected.delete(item.id);
        result.textContent = `${item.filename} · 열기 성공 · 후보 ${data.count}행 · 저장 전`;
      } catch (error) {
        failed++;
        result.textContent = `${item.filename} · 검토 필요 · ${error.message}`;
      }
      $('mail-bulk-results').append(result);
    }
  } finally {
    mailBulkRunning = false;
    $('mail-more').disabled = false;
    $('mail-filter').disabled = false;
    $('mail-bulk-stop').classList.add('hidden');
    for (const input of $('mail-items').querySelectorAll('input,select,button'))
      input.disabled =
        input.matches('[data-mail-select]') &&
        mailItems.some((item) => item.id === input.dataset.mailSelect && item.stage === 'saved');
    updateMailSelection();
    $('mail-bulk-progress').textContent =
      `${mailBulkStop ? '중단' : '검토 종료'} · 성공 ${success}개 · 확인 필요 ${failed}개 · 미처리 ${queue.length - success - failed}개. 거래는 저장하지 않았습니다.`;
    await refreshMail();
  }
};
$('mail-refresh').onclick = async () => {
  if (mailRefreshPending || mailCollecting || mailBulkRunning) return;
  mailRefreshPending = true;
  $('mail-refresh').disabled = true;
  $('mail-refresh').textContent = 'Gmail 수집 요청 중…';
  updateMailSelection();
  try {
    await api('/api/mail/refresh', { method: 'POST' });
    await refreshMail();
  } catch (error) {
    $('mail-state').textContent = error.message;
    notify(error.message, true);
  } finally {
    mailRefreshPending = false;
    $('mail-refresh').disabled = mailCollecting;
    $('mail-refresh').textContent = mailCollecting ? 'Gmail 수집 중…' : '목록 새로고침';
    updateMailSelection();
  }
};
$('mail-items').onclick = async (event) => {
  const button = event.target.closest('[data-mail-open]');
  if (!button) return;
  const id = button.dataset.mailOpen,
    select = [...$('mail-items').querySelectorAll('[data-mail-card]')].find(
      (el) => el.dataset.mailCard === id,
    );
  reviewOrigin = 'mail-inbox';
  const card = select.value,
    name = button.dataset.mailName,
    extension = name.split('.').pop().toLowerCase();
  if (!card) return notify('첨부파일의 카드사를 먼저 선택해 주세요.', true);
  const secure = ['html', 'htm', 'pdf'].includes(extension);
  if (
    (['html', 'htm'].includes(extension) && !['삼성', '현대', '롯데', '우리'].includes(card)) ||
    (extension === 'pdf' && card !== '신한')
  )
    return notify('현재 보안 명세서는 삼성·현대·롯데·우리 HTML과 신한 PDF를 지원합니다.', true);
  button.disabled = true;
  try {
    if (secure && mailAutoPassword) {
      $('secure-card').value = card;
      $('secure-card').dispatchEvent(new Event('change'));
      $('secure-password').value = '';
      $('secure-year').value = $('mail-year').value;
      $('secure-online').checked = card === '삼성' && mailSamsungOnline;
      notify('저장된 공통 암호로 첨부파일을 열고 있습니다.');
      setSecureResult(
        await api(`/api/mail/${encodeURIComponent(id)}/prepare`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ card, year: $('mail-year').value }),
        }),
      );
      navigateTo('review');
      notify('거래 후보를 불러왔습니다. 원문과 건수·금액을 대조한 뒤 저장해 주세요.');
      await refreshMail();
      return;
    }
    const response = await fetch(`/api/mail/${encodeURIComponent(id)}/attachment`, {
      method: 'POST',
      headers: { 'X-Requested-With': 'CardDashboard' },
    });
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.error || '첨부파일을 불러오지 못했습니다.');
    }
    const transfer = new DataTransfer();
    transfer.items.add(new File([await response.blob()], name));
    const field = $(secure ? 'secure-file' : 'file');
    $(secure ? 'secure-card' : 'upload-card').value = card;
    $(secure ? 'secure-card' : 'upload-card').dispatchEvent(new Event('change'));
    field.files = transfer.files;
    field.dispatchEvent(new Event('change'));
    $(secure ? 'secure-year' : 'upload-year').value = $('mail-year').value;
    if (secure) {
      $('secure-password').value = '';
      $('secure-online').checked = false;
    }
    reviewOrigin = 'mail-inbox';
    setUploadMode(secure ? 'secure' : 'spreadsheet');
    navigateTo(secure ? 'secure-upload' : 'upload');
    notify('첨부파일을 불러왔습니다. 카드사·연도·비밀번호를 확인하고 미리보기를 진행하세요.');
    await refreshMail();
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
    button.disabled = false;
  }
};
refreshMail();

setInterval(() => {
  if (
    !document.hidden &&
    !$('mail-refresh').disabled &&
    !$('mail-items').contains(document.activeElement) &&
    !$('mail-items').querySelector('button:disabled')
  )
    refreshMail();
}, 60000);
