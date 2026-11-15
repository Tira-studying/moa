/**
 * 로그인 화면: 아이디와 비밀번호를 서버에 보내고 성공하면 기본 화면으로 이동합니다.
 * 비밀번호는 브라우저 저장소에 저장하지 않고 제출 후 입력칸을 비웁니다.
 * '로그인 유지'를 켜면 서버가 90일짜리 세션 쿠키를 주고, 쓸 때마다 다시 90일로 늘립니다(auth.py).
 */

const form = document.getElementById('login-form'),
  password = document.getElementById('login-password'),
  button = document.getElementById('login-button'),
  status = document.getElementById('login-status');
fetch('/api/session')
  .then((r) => r.json())
  .then((data) => {
    if (data.authenticated) location.replace('/');
    else if (data.configured === false) {
      status.textContent = '서버에서 먼저 로그인 비밀번호를 설정해야 합니다.';
      button.disabled = true;
    }
  })
  .catch(() => {
    status.textContent = '서버에 연결하지 못했습니다.';
  });
form.onsubmit = async (event) => {
  event.preventDefault();
  button.disabled = true;
  status.textContent = '로그인 중입니다.';
  const value = password.value;
  password.value = '';
  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'CardDashboard' },
      body: JSON.stringify({
        username: document.getElementById('login-username').value.trim(),
        password: value,
        remember: document.getElementById('login-remember').checked,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.error || '로그인에 실패했습니다.');
    location.replace('/');
  } catch (error) {
    status.textContent = error.message;
    button.disabled = false;
    password.focus();
  }
};
