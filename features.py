"""사용자별 화면 기능 켜기·끄기: 필요한 기능만 켜서 화면을 단순하게 쓰도록 합니다.

기능은 기본 꺼짐입니다. DEFAULTS에 적은 기능만 처음부터 켜져 있습니다(사용 내역 함께 보기). 꺼도 그 기능에 입력한 설정(카드·예산·급여 등)은 지우지 않습니다.
기능을 끈 사용자에게는 해당 API가 계산 없이 {"enabled": false}만 돌려줍니다.

호출 관계: app.py의 /api/features와 각 기능 API → 이 파일 → user_features 표(migrations.py 3단계)
"""

FEATURES = {
    "card_performance": "카드 실적",
    "tax_deduction": "연말정산 카드 공제",
    "budgets": "분야별 예산",
    "subscriptions": "구독·정기 결제",
    # 켜면 다른 구성원이 내 사용 내역을 읽기 전용으로 봅니다(shared_spending.py).
    "share_transactions": "내 사용 내역 함께 보기",
}
# 저장된 선택이 없을 때 켜진 것으로 보는 기능입니다. 끄면 꺼짐이 저장됩니다.
DEFAULTS = {"share_transactions": True}


def is_enabled(db, owner, feature):
    row = db.execute(
        "SELECT enabled FROM user_features WHERE user_id=? AND feature=?", (owner, feature)
    ).fetchone()
    return bool(row[0]) if row else DEFAULTS.get(feature, False)


def set_enabled(db, owner, feature, enabled):
    if feature not in FEATURES:
        raise ValueError("알 수 없는 기능입니다.")
    db.execute(
        "INSERT INTO user_features(user_id,feature,enabled) VALUES(?,?,?) "
        "ON CONFLICT(user_id,feature) DO UPDATE SET enabled=excluded.enabled",
        (owner, feature, int(bool(enabled))),
    )


def enabled_map(db, owner):
    """화면 시작 때 한 번에 읽는 {기능: 켜짐 여부} 목록입니다."""
    stored = dict(db.execute("SELECT feature,enabled FROM user_features WHERE user_id=?", (owner,)))
    return {
        feature: bool(stored[feature]) if feature in stored else DEFAULTS.get(feature, False)
        for feature in FEATURES
    }
