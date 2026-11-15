/**
 * 홈 화면 앱의 연결 끊김 안내만 제공하는 서비스 워커입니다.
 * 거래·명세서·로그인 화면·API 응답은 캐시에 넣지 않습니다.
 * offline.html을 수정하면 CACHE_NAME의 버전도 올려 새 안내를 설치합니다.
 */
const CACHE_PREFIX = 'moa-pwa-public-';
const CACHE_NAME = `${CACHE_PREFIX}v1`;
const OFFLINE_URL = '/static/offline.html';

// 설치: 사용자 정보가 없는 고정 안내 HTML 하나만 저장합니다.
self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const response = await fetch(OFFLINE_URL, {
        credentials: 'omit',
        cache: 'reload',
        redirect: 'error',
      });
      if (!response.ok || !response.headers.get('Content-Type')?.includes('text/html')) {
        throw new Error('Offline page unavailable');
      }
      const cache = await caches.open(CACHE_NAME);
      await cache.put(OFFLINE_URL, response);
      await self.skipWaiting();
    })(),
  );
});

// 갱신: 이 기능이 만든 예전 안내 캐시만 지웁니다. 화면을 강제로 새로고침하지 않습니다.
self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(
        names
          .filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
          .map((name) => caches.delete(name)),
      );
      await self.clients.claim();
    })(),
  );
});

// 페이지 이동은 항상 서버에 요청합니다. 통신 실패 시에만 고정 안내를 보여줍니다.
// API·업로드 등은 가로채지 않아 오프라인 상태를 정상 저장으로 오인하지 않게 합니다.
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (
    event.request.method !== 'GET' ||
    event.request.mode !== 'navigate' ||
    url.origin !== self.location.origin ||
    url.pathname.startsWith('/api/')
  ) {
    return;
  }
  event.respondWith(
    fetch(event.request).catch(async () => {
      const cache = await caches.open(CACHE_NAME);
      const response = await cache.match(OFFLINE_URL);
      return (
        response ||
        new Response('연결을 확인한 뒤 모아를 다시 열어 주세요.', {
          status: 503,
          headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' },
        })
      );
    }),
  );
});
