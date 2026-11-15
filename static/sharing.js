/**
 * 계정·공동 관리 화면: 소유자의 계정 관리, 본인 암호 변경, 최근 변경 이력을 표시합니다.
 * 화면을 숨기는 것만으로 권한이 생기지 않으며, 최종 권한 검사는 서버가 다시 수행합니다.
 */

let memberSnapshot = null,
  memberSequence = 0;
/** 권한에 맞는 계정 목록과 변경 이력을 표시합니다. */
async function loadMembers() {
  const sequence = ++memberSequence;
  $('members-state').textContent = '계정을 확인하고 있습니다.';
  try {
    const data = await api('/api/members');
    if (sequence !== memberSequence) return;
    memberSnapshot = data;
    $('account-role').textContent = data.user.role === 'owner' ? '● 소유자' : '● 공동관리자';
    $('members-state').textContent =
      `로그인: ${data.user.username} · ${data.user.role === 'owner' ? '소유자' : '공동관리자'} · 개인 설정 번호 ${data.user.id}`;
    $('members-owner').hidden = data.user.role !== 'owner';
    $('codef-settings').hidden = data.user.role !== 'owner';
    if (data.user.role === 'owner' && typeof loadCodefStatus === 'function') loadCodefStatus();
    const managers = data.members.filter((row) => row.role === 'manager');
    $('member-list').innerHTML = managers
      .map(
        (row) =>
          /* HTML */ `<article class="finance-item">
            <strong>${escapeHTML(row.username)}</strong>
            <p>${row.active ? '접근 허용' : '접근 해제됨'}</p>
            <button
              class="button"
              data-member-id="${row.id}"
              data-member-active="${row.active ? 'false' : 'true'}"
            >
              ${row.active ? '접근 해제 · 해당 계정 로그아웃' : '접근 다시 허용'}
            </button>
          </article>`,
      )
      .join('');
    $('member-form').querySelector('button').disabled = managers.some((row) => row.active);
    $('member-reset-id').innerHTML = managers
      .map((row) => `<option value="${row.id}">${escapeHTML(row.username)}</option>`)
      .join('');
    $('member-reset-form').querySelector('button').disabled = !managers.length;
    const audit = await api('/api/audit');
    if (sequence !== memberSequence) return;
    $('audit-list').innerHTML =
      audit.items
        .map(
          (row) =>
            /* HTML */ `<article class="finance-item">
              <strong>${escapeHTML(row.actor_name)} · ${escapeHTML(row.action)}</strong>
              <p class="help">
                ${escapeHTML(koreaTime(row.created))} (한국 시간) · ${escapeHTML(row.target)}
              </p>
            </article>`,
        )
        .join('') || '<p class="help">기록된 변경이 없습니다.</p>';
  } catch (error) {
    if (sequence === memberSequence) $('members-state').textContent = error.message;
  }
}
$('members-refresh').onclick = loadMembers;
/** 계정 화면에서 확인한 버전으로 계정 변경을 요청합니다. */
async function memberRequest(path, payload) {
  return api(path, {
    method: path === '/api/members' ? 'POST' : 'PUT',
    revision: memberSnapshot?._revision,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}
$('member-form').onsubmit = async (event) => {
  event.preventDefault();
  const form = event.target,
    button = form.querySelector('button');
  button.disabled = true;
  try {
    if ($('member-password').value !== $('member-confirm').value)
      throw Error('비밀번호 확인이 일치하지 않습니다.');
    await memberRequest('/api/members', {
      username: $('member-name').value.trim(),
      password: $('member-password').value,
    });
    form.reset();
    await loadMembers();
    notify('공동관리자 계정을 추가했습니다.');
  } catch (error) {
    $('members-state').textContent = error.message;
    button.disabled = false;
  } finally {
    $('member-password').value = '';
    $('member-confirm').value = '';
  }
};
$('member-list').onclick = async (event) => {
  const button = event.target.closest('[data-member-id]');
  if (!button) return;
  button.disabled = true;
  try {
    await memberRequest('/api/members/' + button.dataset.memberId, {
      active: button.dataset.memberActive === 'true',
    });
    await loadMembers();
    notify('공동관리자 접근 권한을 변경했습니다.');
  } catch (error) {
    $('members-state').textContent = error.message;
    button.disabled = false;
  }
};
$('member-reset-form').onsubmit = async (event) => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  button.disabled = true;
  try {
    await memberRequest('/api/members/' + $('member-reset-id').value, {
      password: $('member-reset-password').value,
    });
    await loadMembers();
    notify('비밀번호를 재설정하고 해당 계정의 세션을 종료했습니다.');
  } catch (error) {
    $('members-state').textContent = error.message;
  } finally {
    $('member-reset-password').value = '';
    button.disabled = false;
  }
};
$('own-password-form').onsubmit = async (event) => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  button.disabled = true;
  try {
    if ($('own-new-password').value !== $('own-confirm-password').value)
      throw Error('새 비밀번호 확인이 일치하지 않습니다.');
    await memberRequest('/api/account/password', {
      current_password: $('own-current-password').value,
      password: $('own-new-password').value,
    });
    location.replace('/login');
  } catch (error) {
    $('members-state').textContent = error.message;
    button.disabled = false;
  } finally {
    for (const id of ['own-current-password', 'own-new-password', 'own-confirm-password'])
      $(id).value = '';
  }
};
api('/api/session')
  .then((data) => {
    if (data.user)
      $('account-role').textContent = data.user.role === 'owner' ? '● 소유자' : '● 공동관리자';
  })
  .catch(() => {});
if (!$('sharing').hidden) loadMembers();
