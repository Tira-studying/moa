/** 본인 CODEF 연결과 검토 목록입니다. 연결 ID·토큰·인증 정보는 브라우저로 받지 않습니다. */
let codefSyncSequence = 0;
const codefCardLabels = {
  samsung: '삼성',
  hyundai: '현대',
  kb: 'KB국민',
  shinhan: '신한',
  lotte: '롯데',
  woori: '우리',
};
const codefStateLabels = {
  verifying: '연결 검증 중 · 자동 수집 보류',
  ready: '수집 가능',
  needs_auth: '연결·인증 확인 필요',
  needs_review: '조회 내역 확인 필요 · 로그인 재입력 대상 아님',
  error: '통신·설정 확인 필요',
  quota: '로컬 요청 예산 대기',
};

async function loadCodefSync() {
  const sequence = ++codefSyncSequence;
  try {
    const data = await api('/api/codef-sync');
    if (sequence !== codefSyncSequence) return;
    $('codef-sync-state').textContent =
      data.schedule +
      (data.worker_running
        ? ' · 수집 작업자 실행 중'
        : ' · 수집 작업자 응답 없음. 서버에서 실행 상태를 확인하세요.');
    const links = data.links
      .map(
        (row) => `<article class="finance-item">
      <strong>${escapeHTML(codefCardLabels[row.card] || row.card)}</strong>
      <p>${row.enabled ? escapeHTML(codefStateLabels[row.state] || row.state) : '수집 중지'} · 마지막 성공 ${row.last_success ? escapeHTML(new Date(row.last_success * 1000).toLocaleString('ko-KR')) : '아직 없음'}</p>
      <p>${escapeHTML(row.message)} · 저장 ${row.saved_count}건 · 확인 필요 ${row.review_count}건</p>
      ${row.enabled ? `<button class="button" data-codef-pause="${escapeHTML(row.card)}">수집 중지</button>` : ''}
    </article>`,
      )
      .join('');
    // 수동 조회는 같은 원장에 저장하지만 예약 수집용 연결 ID를 만들지는 않습니다.
    const manual = (data.manual || [])
      .map(
        (row) => `<article class="finance-item">
      <strong>${escapeHTML(codefCardLabels[row.card] || row.card)} · 수동 조회·저장</strong>
      <p>마지막 수동 조회 ${escapeHTML(new Date(row.last_success * 1000).toLocaleString('ko-KR'))} · 승인 묶음 ${row.group_count}건</p>
      <p>저장 ${row.saved_count}건 · 확인 필요 ${row.review_count}건</p>
      <p class="help">이 기록은 예약 수집 연결을 만들지 않습니다. 수동 조회는 터미널에서 직접 실행합니다.</p>
    </article>`,
      )
      .join('');
    $('codef-sync-links').innerHTML =
      links + manual ||
      '<p class="help">아직 본인 계정에 저장된 카드 연결이 없습니다. 일회성 조회 시험과 자동 수집 연결은 별개입니다.</p>';
    $('codef-sync-review').innerHTML =
      data.review
        .map(
          (row) => `<article class="finance-item">
      <strong>${escapeHTML(codefCardLabels[row.card] || row.card)} · ${escapeHTML(row.used_on)}</strong>
      <p>${escapeHTML(row.merchant)} · ${row.net === null ? '금액 확인 필요' : won(row.net) + '원'}</p>
      <p>${escapeHTML(row.reason)}</p>
      ${row.reason === '명세서와 중복 후보' && row.kind === '0' && row.net !== null ? `<button class="button" data-codef-save="${row.id}">별도 거래로 저장</button>` : ''}
      <button class="button" data-codef-ignore="${row.id}">검토에서 제외 · 기존 원장 유지</button>
    </article>`,
        )
        .join('') || '<p class="help">확인이 필요한 내역이 없습니다.</p>';
  } catch (error) {
    if (sequence === codefSyncSequence) $('codef-sync-state').textContent = error.message;
  }
}

$('codef-sync-refresh').onclick = loadCodefSync;
$('codef-sync-panel').addEventListener('toggle', () => {
  if ($('codef-sync-panel').open) loadCodefSync();
});
$('codef-sync-request').onclick = async () => {
  try {
    await api('/api/codef-sync/refresh', { method: 'POST' });
    notify('수집 작업자에 요청했습니다. 인증 대기 카드는 카드·계좌 연결에서 확인하세요.');
    await loadCodefSync();
  } catch (error) {
    notify(error.message, true);
  }
};
$('codef-sync-panel').addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  const pause = button.dataset.codefPause;
  const id = button.dataset.codefSave || button.dataset.codefIgnore;
  if (!pause && !id) return;
  button.disabled = true;
  try {
    await api(pause ? '/api/codef-sync/pause' : '/api/codef-sync/review/' + id, {
      method: 'POST',
      body: JSON.stringify(
        pause ? { card: pause } : { action: button.dataset.codefSave ? 'save' : 'ignore' },
      ),
    });
    await loadCodefSync();
  } catch (error) {
    notify(error.message, true);
    button.disabled = false;
  }
});
setInterval(() => {
  if (!document.hidden && !$('sharing').hidden && $('codef-sync-panel').open) loadCodefSync();
}, 30000);
