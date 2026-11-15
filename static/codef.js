/**
 * 소유자의 CODEF 개발자 인증 확인 화면입니다. 키·토큰 입력이나 원문 조회 기능은 없습니다.
 * 이 파일은 인증 결과만 표시하며, 개인 연결·수집 상태는 codef-sync.js에서 따로 표시합니다.
 */
let codefChecking = false;
let codefConfigured = false;
let codefStatusSequence = 0;

function renderCodefStatus(data) {
  codefConfigured = data.configured;
  const checked = data.checked_at
    ? ' · 최근 확인 ' + new Date(data.checked_at * 1000).toLocaleString('ko-KR')
    : '';
  $('codef-state').textContent = data.message + checked;
  $('codef-check').disabled = codefChecking || !data.configured;
}

async function loadCodefStatus() {
  if ($('codef-settings').hidden) return;
  const sequence = ++codefStatusSequence;
  try {
    const data = await api('/api/codef/status');
    if (sequence === codefStatusSequence && !codefChecking) renderCodefStatus(data);
  } catch (error) {
    if (sequence === codefStatusSequence) $('codef-state').textContent = error.message;
  }
}

$('codef-refresh').onclick = loadCodefStatus;
$('codef-check').onclick = async () => {
  if (codefChecking) return;
  codefChecking = true;
  ++codefStatusSequence;
  $('codef-check').disabled = true;
  $('codef-state').textContent = 'CODEF 개발자 인증을 확인하고 있습니다.';
  try {
    const data = await api('/api/codef/check', { method: 'POST' });
    renderCodefStatus(data);
    notify(data.message, !data.ok);
  } catch (error) {
    $('codef-state').textContent = error.message;
  } finally {
    codefChecking = false;
    $('codef-check').disabled = !codefConfigured;
  }
};
if (!$('sharing').hidden && !$('codef-settings').hidden) loadCodefStatus();
