"""웹 서버와 별도로 실행되는 Gmail 수집 프로세스입니다.

평소에는 5분마다 수집하고, 새로고침 요청 대기열은 1초마다 확인합니다.
파일 잠금으로 수집 프로세스가 중복 실행되는 것을 막습니다.
"""

import fcntl
import time
from pathlib import Path
from app import connect, app
from gmail_inbox import process_refresh
from personal_data import personal_scope


def tick(connect, next_poll, *, now=None):
    """이번 시점에 필요한 수집을 한 번 처리하고 다음 정기 수집 시각을 반환합니다."""
    now = time.monotonic() if now is None else now
    try:
        result = process_refresh(connect, automatic=now >= next_poll)
        if result is None:
            return next_poll
        if result["ok"]:
            print("Gmail poll completed; candidates:", result["found"], flush=True)
        else:
            print("Gmail poll failed; check the app connection status.", flush=True)
        return time.monotonic() + 300
    except Exception:
        if now >= next_poll:
            print("Gmail poll failed; check the app connection status.", flush=True)
        return now + 300 if now >= next_poll else next_poll


def tick_users(connect, next_polls):
    """활성 계정별 Gmail 설정과 다음 수집 시각을 분리합니다."""
    db = connect()
    try:
        users = [row[0] for row in db.execute("SELECT id FROM users WHERE active=1")]
    finally:
        db.close()
    for user_id in users:
        with personal_scope(user_id):
            next_polls[user_id] = tick(connect, next_polls.get(user_id, 0))
    return {user_id: next_polls[user_id] for user_id in users}


if __name__ == "__main__":
    path = Path(app.config["DATABASE"]).parent
    path.mkdir(parents=True, exist_ok=True)
    with (path / "mail-poller.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        next_polls = {}
        while True:
            next_polls = tick_users(connect, next_polls)
            time.sleep(1)
