"""Gmail과 독립적으로 한국시간 하루 4회 CODEF를 수집합니다.

DB 폴더의 프로세스 잠금으로 중복 작업자를 막고 회차별 DB 작업 키로 재시작 중복을
막습니다. 로그에는 건별 금융 정보·인증 정보·외부 예외 원문을 남기지 않습니다.
"""

import fcntl
from pathlib import Path
import time

from app import app, connect
from codef_sync import Client, tick
from codef_bank import tick as bank_tick

if __name__ == "__main__":
    root = Path(app.config["DATABASE"]).parent
    root.mkdir(parents=True, exist_ok=True)
    with (root / "codef-worker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        client = Client(connect)
        while True:
            try:
                tick(connect, client)
                bank_tick(connect, client)
            except Exception:
                print(
                    "CODEF 작업을 완료하지 못했습니다. 앱의 개인 연결 상태를 확인해 주세요.",
                    flush=True,
                )
            time.sleep(20)
