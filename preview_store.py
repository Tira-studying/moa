"""저장 전 미리보기 보관소: 명세서 표를 DB에 저장하기 전까지 서버 메모리에만 잠시 둡니다.

왜 메모리인가: 미리보기에는 암호를 해제한 명세서 표가 들어 있습니다. 사용자가 저장을 누르지
않은 자료가 DB·디스크·백업에 남지 않게 하려고 일부러 프로세스 메모리에만 둡니다.
그래서 gunicorn 워커는 1개(스레드 여러 개)로 실행합니다. 워커를 늘리려면 같은 사용자의 요청이
같은 워커로 가게 하거나(sticky session), 암호화한 공유 저장소를 따로 설계해야 합니다.

보관 규칙
- 유효 시간(ttl)이 지나면 자동으로 지우고, 읽을 때도 만료 여부를 다시 확인합니다.
- 사용자별 개수 제한: 한 사람이 많이 올려도 다른 사람의 미리보기가 밀려나지 않습니다.
- 전체 개수 제한: 메모리 사용량의 상한입니다. 넘치면 가장 오래된 것부터 지웁니다.

호출 관계: app.py의 /api/preview·/api/secure-html·/api/secure-preview가 put(),
/api/secure-preview·/api/import가 get()을 사용합니다.
"""

import threading
import time
import uuid
from collections import OrderedDict

# 각 항목에 함께 저장하는 만료 시각(time.monotonic 기준 초)의 키 이름입니다.
EXPIRES = "_expires"


class PreviewStore(OrderedDict):
    """토큰 → 미리보기 자료(dict)를 담는 사전입니다. 오래된 순서를 기억합니다.

    테스트와 기존 코드가 사전처럼 비우기·조회를 하므로 OrderedDict를 그대로 상속합니다.
    새 코드는 put()/get()/discard()를 사용합니다.
    """

    def __init__(self, ttl, per_owner, total):
        super().__init__()
        self.ttl = ttl
        self.per_owner = per_owner
        self.total = total
        self.lock = threading.RLock()

    def put(self, owner_id, item):
        """새 미리보기를 보관하고 임의 토큰을 돌려줍니다. 유효 시간 뒤 자동 삭제를 예약합니다."""
        token = uuid.uuid4().hex
        with self.lock:
            self._drop_expired()
            self[token] = {**item, "owner_id": owner_id, EXPIRES: time.monotonic() + self.ttl}
            self._enforce_limits(owner_id)
        # 요청이 더 오지 않아도 해제한 표가 메모리에 오래 남지 않게 정해진 시각에 지웁니다.
        timer = threading.Timer(self.ttl, self.discard, args=(token,))
        timer.daemon = True
        timer.start()
        return token

    def get_for(self, token, owner_id):
        """토큰의 주인이 owner_id일 때만 자료를 돌려줍니다. 없거나 만료됐거나 남의 것이면 None입니다."""
        with self.lock:
            item = self.get(token)
            if item is None:
                return None
            if item.get(EXPIRES, float("inf")) <= time.monotonic():
                self.pop(token, None)
                return None
            # owner_id가 없는 예전 형식은 소유자(1번) 것으로 봅니다.
            if item.get("owner_id", 1) != owner_id:
                return None
            return item

    def discard(self, token):
        """토큰이 있으면 지웁니다. 이미 지워졌어도 오류를 내지 않습니다."""
        with self.lock:
            self.pop(token, None)

    def _drop_expired(self):
        now = time.monotonic()
        for token in [t for t, item in self.items() if item.get(EXPIRES, float("inf")) <= now]:
            del self[token]

    def _enforce_limits(self, owner_id):
        owned = [t for t, item in self.items() if item.get("owner_id", 1) == owner_id]
        for token in owned[: max(0, len(owned) - self.per_owner)]:
            del self[token]
        while len(self) > self.total:
            self.popitem(last=False)
