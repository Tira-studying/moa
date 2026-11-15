/** 본인 카드·계좌 인증 화면입니다. 입력·진행 정보는 브라우저 저장소에 보관하지 않습니다. */
let connectionOptions = null;
let connectionRevision = null;
let connectionJob = null;
let connectionSending = false;
let connectionPoll = null;
let connectionLoadSequence = 0;
let connectionCompleted = null;

/** 화면을 벗어나거나 전송하면 민감 입력을 비웁니다. 선택한 기관만 유지합니다. */
function clearConnectionInputs() {
  document
    .querySelectorAll('#connection-form input,#connection-challenge-form input')
    .forEach((input) => {
      if (input.type === 'checkbox') input.checked = false;
      else input.value = '';
    });
}

function updateConnectionFields() {
  const bank = $('connection-kind').value === 'bank';
  const provider = $('connection-provider').value;
  for (const [id, visible] of [
    ['connection-hyundai', !bank && provider === 'hyundai'],
    ['connection-kb', !bank && provider === 'kb'],
    ['connection-bank', bank],
    ['connection-lotte', !bank && provider === 'lotte'],
  ]) {
    $(id).hidden = !visible;
    $(id)
      .querySelectorAll('input,select')
      .forEach((input) => {
        input.disabled = !visible;
        input.required = visible;
      });
  }
  const stored = bank
    ? '자동 수집에 필요한 연결 ID·계좌번호·생년월일·RSA 암호화 계좌 비밀번호를 모아 DB에 다시 암호화해 보관합니다. 입출금은 별도 보관하며 소비로 선택한 출금만 합산합니다.'
    : provider === 'hyundai'
      ? '자동 수집을 위해 연결 ID와 카드번호·RSA 암호화 PIN을 모아 DB에 다시 암호화해 보관합니다. PIN 평문은 보관하지 않습니다. 같은 암호문으로 조회가 가능한지 확인한 뒤 연결합니다.'
      : provider === 'kb'
        ? '자동 수집을 위해 연결 ID와 KB 카드번호·RSA 암호화한 비밀번호 앞 2자리를 모아 DB에 다시 암호화해 보관합니다. 소지 확인 조회가 성공한 경우에만 저장하며, KB가 하루 첫 조회에서 소지 확인을 요구할 때만 한 번 씁니다.'
        : '모아에는 연결 ID만 암호화해 보관합니다. 검증한 최근 30일 지출을 사용 내역에 저장합니다.';
  $('connection-consent-text').textContent =
    '입력한 본인 정보는 CODEF를 통해 금융기관 인증에 사용합니다. CODEF는 로그인 정보를 관리하는 연결 ID를 발급합니다. 홈페이지 로그인 비밀번호와 추가 인증번호는 모아에 저장하지 않습니다. ' +
    stored;
  $('connection-consent').checked = false;
}

function updateConnectionProviders() {
  if (!connectionOptions) return;
  const providers =
    $('connection-kind').value === 'bank' ? connectionOptions.banks : connectionOptions.cards;
  $('connection-provider').innerHTML = providers
    .map((item) => `<option value="${escapeHTML(item.key)}">${escapeHTML(item.name)}</option>`)
    .join('');
  clearConnectionInputs();
  updateConnectionFields();
}

/** 서버가 확인한 단계만 표시합니다. 조회·복귀·네트워크 복구로 인증을 재전송하지 않습니다. */
function renderConnectionJob(data) {
  const changedChallenge = connectionJob?.challenge_id !== data?.challenge_id;
  connectionJob = data;
  const active = data && ['working', 'needs_input'].includes(data.state);
  $('connection-form').hidden = Boolean(data);
  $('connection-inputs').disabled = Boolean(
    active || connectionSending || connectionRevision === null || !connectionOptions?.configured,
  );
  $('connection-progress').hidden = !data;
  $('connection-progress-message').textContent = data?.message || '';
  $('connection-cancel').hidden = !active;
  $('connection-new').hidden = !data || Boolean(active);
  $('connection-challenge-form').hidden = data?.state !== 'needs_input';
  $('connection-answer').disabled = connectionSending;
  if (data?.state === 'needs_input') {
    const kb = data.method === 'kb_possession';
    const simple = data.method === 'simpleAuth';
    $('connection-code-label').hidden = kb || simple;
    $('connection-code').disabled = kb || simple;
    $('connection-code').required = !kb && !simple;
    $('connection-code').inputMode = data.method === 'smsAuthNo' ? 'numeric' : 'text';
    $('connection-code').pattern =
      data.method === 'smsAuthNo' ? '[0-9]{1,32}' : '[A-Za-z0-9]{1,32}';
    $('connection-kb-auth').hidden = !kb;
    for (const id of ['connection-kb-number', 'connection-kb-pin']) {
      $(id).disabled = !kb;
      $(id).required = kb;
    }
    $('connection-answer').textContent = simple
      ? '앱에서 인증을 마쳤어요'
      : kb
        ? '카드 소지 확인하기'
        : '인증번호 확인';
    $('connection-progress-message').textContent = simple
      ? '금융기관의 앱 인증을 직접 완료한 뒤 아래 버튼을 눌러 주세요.'
      : data.method === 'emailAuthNo'
        ? '금융기관에서 보낸 이메일 인증번호를 입력해 주세요.'
        : kb
          ? 'KB 카드번호와 비밀번호 앞 2자리를 입력해 주세요.'
          : '금융기관에서 보낸 문자 인증번호를 입력해 주세요.';
    if (changedChallenge) {
      $('connection-code').value = '';
      $('connection-kb-number').value = '';
      $('connection-kb-pin').value = '';
      // 소지 확인 정보 저장은 기본으로 켜 둡니다. 끄면 이번 확인에만 씁니다.
      $('connection-kb-remember').checked = true;
    }
  }
  clearTimeout(connectionPoll);
  connectionPoll = null;
  if (active && !document.hidden) connectionPoll = setTimeout(pollConnectionJob, 1500);
  if (data && !active && connectionCompleted !== data.id) {
    connectionCompleted = data.id;
    connectionRevision = null;
    $('connection-inputs').disabled = true;
    const reads = [loadConnectionSetup()];
    if (data.state === 'success') reads.push(refresh(), loadCodefSync(), loadBank());
    Promise.all(reads).catch(() => {});
  }
}

async function pollConnectionJob() {
  const id = connectionJob?.id;
  if (!id || document.hidden) return;
  try {
    const data = await api(`/api/connections/setup/${id}`);
    if (connectionJob?.id === id) renderConnectionJob(data);
  } catch (error) {
    $('connection-state').textContent =
      error.message + ' 진행 상태 확인으로 이어갈 수 있습니다. 인증 요청은 재전송하지 않습니다.';
  }
}

async function loadConnectionSetup() {
  const sequence = ++connectionLoadSequence;
  try {
    const data = await api('/api/connections');
    if (sequence !== connectionLoadSequence) return;
    const first = !connectionOptions;
    connectionOptions = data;
    connectionRevision = data._revision;
    $('connection-state').textContent = data.configured
      ? '본인 휴대폰에서 인증을 진행하세요. 연결 후 한국시간 00·08·14·20시에 자동 수집합니다.'
      : '서버의 금융 연결 설정을 확인해야 합니다. 입력은 아직 받지 않습니다.';
    if (first) updateConnectionProviders();
    if (
      data.current &&
      (connectionJob?.id === data.current.id ||
        ['working', 'needs_input'].includes(data.current.state))
    )
      renderConnectionJob(data.current);
    else renderConnectionJob(null);
  } catch (error) {
    $('connection-state').textContent = error.message;
    $('connection-inputs').disabled = true;
  }
}

$('connection-kind').onchange = updateConnectionProviders;
$('connection-provider').onchange = () => {
  clearConnectionInputs();
  updateConnectionFields();
};
$('connection-status-refresh').onclick = loadConnectionSetup;
$('connection-form').onsubmit = async (event) => {
  event.preventDefault();
  if (connectionSending || !connectionOptions?.configured) return;
  const payload = {
    nonce: crypto.randomUUID(),
    kind: $('connection-kind').value,
    provider: $('connection-provider').value,
    consent: $('connection-consent').checked,
    login_id: $('connection-login-id').value,
    password: $('connection-password').value,
  };
  if (payload.kind === 'bank')
    Object.assign(payload, {
      account: $('connection-account').value,
      birth_date: $('connection-birth').value,
      pin: $('connection-account-pin').value,
    });
  else if (payload.provider === 'hyundai')
    Object.assign(payload, {
      card_number: $('connection-card-number').value,
      pin: $('connection-card-pin').value,
    });
  else if (payload.provider === 'kb')
    Object.assign(payload, {
      kb_card_number: $('connection-kb-card-number').value,
      kb_pin: $('connection-kb-card-pin').value,
    });
  else if (payload.provider === 'lotte')
    Object.assign(payload, {
      user_name: $('connection-name').value,
      identity: $('connection-identity').value,
      phone_no: $('connection-phone').value,
      telecom: $('connection-telecom').value,
    });
  connectionSending = true;
  $('connection-inputs').disabled = true;
  clearConnectionInputs();
  try {
    const body = JSON.stringify(payload);
    for (const key of Object.keys(payload)) delete payload[key];
    const data = await api('/api/connections/setup', {
      method: 'POST',
      revision: connectionRevision,
      headers: { 'Content-Type': 'application/json' },
      body,
    });
    renderConnectionJob(data);
    $('connection-progress').scrollIntoView({ block: 'start' });
  } catch (error) {
    $('connection-state').textContent = error.message;
    // 응답이 유실됐어도 새 금융 요청 대신 현재 작업만 조회합니다.
    await loadConnectionSetup();
    notify(error.message, true);
  } finally {
    connectionSending = false;
    renderConnectionJob(connectionJob);
  }
};
$('connection-challenge-form').onsubmit = async (event) => {
  event.preventDefault();
  if (connectionSending || connectionJob?.state !== 'needs_input') return;
  const job = connectionJob;
  const answer =
    job.method === 'kb_possession'
      ? {
          number: $('connection-kb-number').value,
          pin: $('connection-kb-pin').value,
          remember: $('connection-kb-remember').checked,
        }
      : job.method === 'simpleAuth'
        ? '1'
        : $('connection-code').value;
  const body = JSON.stringify({ challenge_id: job.challenge_id, answer });
  clearConnectionInputs();
  connectionSending = true;
  $('connection-answer').disabled = true;
  try {
    await api(`/api/connections/setup/${job.id}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    });
    await pollConnectionJob();
  } catch (error) {
    notify(error.message, true);
  } finally {
    connectionSending = false;
    $('connection-answer').disabled = false;
  }
};
$('connection-cancel').onclick = async () => {
  if (!connectionJob) return;
  $('connection-cancel').disabled = true;
  try {
    const data = await api(`/api/connections/setup/${connectionJob.id}/cancel`, { method: 'POST' });
    clearConnectionInputs();
    renderConnectionJob(data);
    await loadConnectionSetup();
  } catch (error) {
    notify(error.message, true);
  } finally {
    $('connection-cancel').disabled = false;
  }
};
$('connection-new').onclick = () => {
  connectionJob = null;
  clearConnectionInputs();
  loadConnectionSetup();
};
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearConnectionInputs();
    clearTimeout(connectionPoll);
  } else if (connectionJob && ['working', 'needs_input'].includes(connectionJob.state))
    pollConnectionJob();
});
window.addEventListener('pagehide', clearConnectionInputs);
loadConnectionSetup.ready = true;
if (!$('connections').hidden) loadConnectionSetup();
