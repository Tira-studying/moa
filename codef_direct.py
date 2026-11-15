"""계정 등록 없이 승인 상품 API로 수동 조회·저장합니다. 예약 수집 연결을 만들지 않습니다.

문서에 명시된 직접 로그인과 같은 상품 URL의 SMS 추가 인증을 사용합니다. 비밀번호·PIN·
본인 정보·인증 세션은 메모리에서만 사용하고, 결과는 기존 검증·원장 저장 규칙을 따릅니다.
"""

from datetime import datetime, timedelta
import hmac
import time

from codef_card import CARD_OPTIONS, query_approvals
from codef_connection import configuration, config_digest
from codef_sync import KST, SyncError, normalize, storage_key, store_events, touch_revision


def schema(db):
    """마지막 수동 조회 시각·그룹 수만 기록합니다. 연결 ID나 인증 정보는 없습니다."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS codef_manual_runs(owner_id INTEGER NOT NULL REFERENCES users(id),card TEXT NOT NULL,last_success INTEGER NOT NULL,group_count INTEGER NOT NULL,PRIMARY KEY(owner_id,card))"
    )
    db.commit()


def link_version(db, owner, card):
    """수동 조회 중 다른 작업에서 기존 연결을 바꾸거나 중지하면 결과를 폐기하기 위한 값입니다."""
    row = db.execute(
        "SELECT version FROM codef_links WHERE owner_id=? AND card=?", (owner, card)
    ).fetchone()
    return row[0] if row else None


def check_configuration(initial_key, settings):
    """조회 중 비밀 키가 교체됐으면 다른 식별자로 중복 저장되지 않도록 중단합니다."""
    if not hmac.compare_digest(initial_key, storage_key()) or config_digest(
        settings
    ) != config_digest(configuration()):
        raise SyncError("처리 중 CODEF 키 설정이 바뀌어 저장하지 않았습니다.")


def collect_direct(connect, client, owner, card, operator):
    """개인 계정·연결 버전을 조회 전후 검사하고 같은 출처의 지출을 중복 없이 저장합니다."""
    if card not in CARD_OPTIONS:
        raise SyncError("지원 카드사를 확인해 주세요.")
    initial_key = storage_key()
    db = connect()
    try:
        if not db.execute("SELECT 1 FROM users WHERE id=? AND active=1", (owner,)).fetchone():
            raise SyncError("활성 모아 계정을 확인해 주세요.")
        version = link_version(db, owner, card)
    finally:
        db.close()
    settings, token = client.credentials()
    today = datetime.now(KST).date()
    start, end = (today - timedelta(days=6)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
    print("승인내역 직접 조회 · 최근 7일의 검증된 지출을 수동 저장합니다.")
    print(
        "자동 수집 연결은 만들지 않습니다. 로그인·본인 정보는 CODEF에 전송하며 로컬에 저장하지 않습니다."
    )
    payload = operator["read_card_payload"](settings, card, start, end)
    try:
        data = query_approvals(token, payload, operator["answer_challenge"], sender=client.send)
    finally:
        payload.clear()
    check_configuration(initial_key, settings)
    items = normalize(data, card, start, end)
    del data
    db = connect()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM users WHERE id=? AND active=1", (owner,)
            ).fetchone() or version != link_version(db, owner, card):
                raise SyncError("계정 또는 연결이 변경되어 수동 조회 결과를 저장하지 않았습니다.")
            # 정규화가 식별자 키를 읽고 DB를 다시 여는 동안의 설정 변경도 폐기합니다.
            check_configuration(initial_key, settings)
            changed = store_events(db, owner, card, items)
            db.execute(
                "INSERT INTO codef_manual_runs VALUES(?,?,?,?) ON CONFLICT(owner_id,card) DO UPDATE SET last_success=excluded.last_success,group_count=excluded.group_count",
                (owner, card, int(time.time()), len(items)),
            )
            touch_revision(db, owner, "CODEF 직접 조회 저장" if changed else "CODEF 직접 조회 확인")
            return len(items)
    finally:
        db.close()
