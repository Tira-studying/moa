/**
 * PWA 첫 단계: 홈 화면 설치 안내, 연결 상태 표시, 복귀 시 로그인 확인을 담당합니다.
 * 설치 이벤트와 상태는 메모리에서만 관리합니다. 거래·암호를 브라우저 저장소에 쓰지 않습니다.
 * 로그인 화면에서도 실행되므로 다른 앱 스크립트의 전역 변수에 의존하지 않습니다.
 */
(() => {
  let installPrompt = null;
  let installedThisSession = false;
  let sessionCheckSequence = 0;
  let sessionController = null;
  // 페이지를 처음 받은 계정을 기억해야 다른 탭에서 계정을 바꾼 경우도 감지할 수 있습니다.
  const pageUser = /^\d+$/.test(document.body.dataset.userId || '')
    ? document.body.dataset.userId
    : null;
  const installButton = document.getElementById('pwa-install');
  const installStatus = document.getElementById('pwa-install-status');
  const networkStatus = document.getElementById('pwa-network-status');
  const isAppleMobile =
    /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

  /** 브라우저가 제공하는 설치 기능에 맞춰 버튼 또는 수동 설치 경로를 안내합니다. */
  function showInstallState() {
    if (!installStatus) return;
    const standalone =
      window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
    if (standalone) {
      installStatus.textContent =
        '홈 화면 앱으로 실행 중입니다. 내역을 보려면 서버 연결이 필요합니다.';
    } else if (installedThisSession) {
      installStatus.textContent = '설치했습니다. 홈 화면의 모아 아이콘으로 열어 주세요.';
    } else if (isAppleMobile) {
      installStatus.textContent =
        'Safari의 공유 메뉴 → 홈 화면에 추가를 선택하세요. 웹 앱으로 열기가 보이면 켜 주세요.';
    } else if (installPrompt) {
      installStatus.textContent = '아래 버튼으로 모아를 설치해 앱처럼 열 수 있습니다.';
    } else {
      installStatus.textContent =
        'Chrome 메뉴의 앱 설치 또는 홈 화면에 추가를 선택하세요. 설치 메뉴는 브라우저에 따라 다를 수 있습니다.';
    }
    if (installButton) installButton.hidden = !installPrompt || standalone || installedThisSession;
  }

  /** 온라인 표시만으로 Tailscale 연결까지 확인되는 것은 아니므로 서버 실패도 함께 안내합니다. */
  function showConnectionFailure(failed) {
    if (networkStatus) networkStatus.hidden = !failed;
  }

  /** 앱으로 돌아왔을 때 만료·접근 해제된 세션으로 이전 화면을 계속 보지 않게 합니다. */
  async function checkReturningSession() {
    if (location.pathname === '/login') return;
    const sequence = ++sessionCheckSequence;
    // 복귀 이벤트가 연달아 오면 이전 확인을 취소합니다. 계정 전환 뒤의 최신 확인만 반영합니다.
    sessionController?.abort();
    sessionController = null;
    if (pageUser) document.body.classList.add('session-check-pending');
    if (!navigator.onLine) {
      showConnectionFailure(true);
      return;
    }
    let verified = false;
    const controller = new AbortController();
    sessionController = controller;
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch('/api/session', {
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      });
      if (!response.ok) throw new Error('Connection unavailable');
      const data = await response.json();
      if (sequence !== sessionCheckSequence) return;
      if (!data.authenticated) location.replace('/login');
      else if (pageUser && String(data.user?.id) !== pageUser) location.replace('/');
      else {
        verified = true;
        showConnectionFailure(false);
      }
    } catch {
      if (sequence === sessionCheckSequence) showConnectionFailure(true);
    } finally {
      clearTimeout(timer);
      if (sequence === sessionCheckSequence) {
        sessionController = null;
        if (verified) document.body.classList.remove('session-check-pending');
      }
    }
  }

  document.getElementById('pwa-reconnect')?.addEventListener('click', () => {
    if (location.pathname === '/login') location.reload();
    else checkReturningSession();
  });

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    installPrompt = event;
    showInstallState();
  });
  installButton?.addEventListener('click', async () => {
    if (!installPrompt) return;
    installButton.disabled = true;
    try {
      await installPrompt.prompt();
      const choice = await installPrompt.userChoice;
      installedThisSession = choice.outcome === 'accepted';
    } catch {
      // 브라우저 설치 창이 취소·종료돼도 수동 설치 안내를 다시 사용할 수 있습니다.
    } finally {
      installPrompt = null;
      installButton.disabled = false;
      showInstallState();
    }
  });
  window.addEventListener('appinstalled', () => {
    installedThisSession = true;
    installPrompt = null;
    showInstallState();
  });
  window.addEventListener('offline', () => showConnectionFailure(true));
  window.addEventListener('online', () => {
    showConnectionFailure(false);
    checkReturningSession();
  });
  window.addEventListener('focus', checkReturningSession);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') checkReturningSession();
  });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) checkReturningSession();
  });

  showInstallState();
  showConnectionFailure(!navigator.onLine);
  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' }).catch(() => {
      // 설치 지원이 제한돼도 기존 온라인 웹 화면과 로그인은 계속 사용할 수 있습니다.
    });
  }
})();
