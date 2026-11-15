"""개인 원장과 공동 계정 관리를 안전하게 수정하도록 요청 전체를 하나의 저장 단위로 묶습니다.

처리 순서: 세션·권한 재확인 → 개인/계정 관리 버전 비교 → API 실행 → 해당 범위의 이력과 버전 저장.
중간에 실패하면 거래·버전·이력을 모두 되돌립니다. RequestConnection은 내부 API가
이 저장 단위를 먼저 끝내지 못하게 하는 연결 대리 객체입니다.
"""

import re
import hashlib
import time
from flask import g, request, jsonify
from personal_data import current_user_id
import bank_expenses
from werkzeug.security import generate_password_hash, check_password_hash

MUTATIONS = {
    "bank_consumption_update": "계좌 소비 선택 변경",
    "bank_review_update": "계좌 출금 지출 확인 변경",
    "bank_refresh": "우리은행 입출금 수집 요청",
    "bank_pause": "우리은행 개인 연결 중지",
    "category_reviews_update": "소비 분야 확인 완료",
    "codef_sync_refresh": "CODEF 수동 수집 요청",
    "codef_sync_pause": "CODEF 개인 연결 중지",
    "codef_sync_review": "CODEF 개인 지출 검토",
    "sms_key_create": "문자 수신 키 발급·교체",
    "sms_key_revoke": "문자 수신 키 해제",
    "sms_event_update": "문자 임시내역 대조 상태 변경",
    "import_file": "명세서 저장",
    "delete_import": "명세서 삭제",
    "update_category": "거래 분류·상세 수정",
    "category_settings_create": "개인 소비 분야 추가",
    "category_settings_update": "개인 소비 분야 이름 변경",
    "category_settings_delete": "개인 소비 분야 삭제",
    "installments": "할부 일정 변경",
    "transaction_exclusion": "거래 집계 포함·제외 변경",
    "recurring_decision": "고정 지출 확인 변경",
    "billing_card_create": "개인 카드사 추가",
    "billing_settings": "카드 사용·결제일 설정",
    "billing_update": "월별 청구·납부 기록 변경",
    "card_performance_settings": "카드 실적 켜기·끄기",
    "features_update": "화면 기능 켜기·끄기",
    "tax_deduction_settings": "연말정산 카드 공제 설정",
    "budgets_update": "분야별 월 예산 변경",
    "card_product_create": "카드 실적 설정 추가",
    "card_product_update": "카드 실적 설정 변경",
    "card_product_delete": "카드 실적 설정 삭제",
    "card_product_assign": "명세서 카드 연결 변경",
    "manual_entry_create": "직접 입력 지출 추가",
    "manual_entry_update": "직접 입력 지출 수정",
    "manual_entry_delete": "직접 입력 지출 삭제",
    "receipt_upload": "영수증 사진 추가",
    "receipt_delete": "영수증 사진 삭제",
    "member_create": "공동관리자 계정 추가",
    "member_update": "공동관리자 접근·비밀번호 변경",
    "own_password": "본인 비밀번호 변경",
}
READS = {
    "bank_review_data",
    "bank_data",
    "category_reviews_data",
    "category_settings_data",
    "codef_sync_data",
    "codef_status",
    "sms_data",
    "get_data",
    "export_transactions",
    "finance_data",
    "billing_data",
    "card_performance_data",
    "features_data",
    "tax_deduction_data",
    "budgets_data",
    "subscriptions_data",
    "members",
    "audit_history",
    "household_data",
    "shared_management_data",
    "shared_transactions_data",
    "receipt_image",
    "shared_receipt_image",
}
ACCOUNT_ENDPOINTS = {"members", "member_create", "member_update", "own_password"}


# 1. 요청 전체의 트랜잭션을 유지하는 대리 연결
# ─────────────────────────────────────────────────────────────────
class RequestConnection:
    """요청 전체의 저장 단위를 유지하도록 내부 코드의 조기 commit·close를 막는 대리 연결입니다."""

    def __init__(self, db):
        self.db = db

    def execute(self, sql, *args):
        """개별 API가 BEGIN을 다시 요청해도 이미 시작한 저장 단위를 그대로 사용합니다."""
        if sql.strip().upper().startswith("BEGIN"):
            return self.db.execute("SELECT 1")
        return self.db.execute(sql, *args)

    def __getattr__(self, key):
        return getattr(self.db, key)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        """실제 연결 종료는 요청 공통 마무리에 맡깁니다."""
        pass

    def commit(self):
        """개별 API가 먼저 저장을 확정하지 못하도록 공통 마무리까지 기다립니다."""
        pass

    def rollback(self):
        """실패한 요청의 실제 취소는 공통 마무리 또는 예외 정리에서 수행합니다."""
        pass


# 2. 권한·버전 확인과 공동 계정 API
# ─────────────────────────────────────────────────────────────────
def install_collaboration(app, connect):
    """공동 수정 검사·저장 마무리·계정 관리 API를 앱에 등록합니다."""

    @app.before_request
    def shared_transaction():
        """DB 잠금을 얻은 뒤 세션·소유자 권한·화면 버전을 다시 확인하고 하나의 저장 단위를 시작합니다."""
        endpoint = request.endpoint
        if endpoint not in MUTATIONS and endpoint not in READS:
            return
        if not getattr(g, "user", None):
            return
        db = connect()
        try:
            write = endpoint in MUTATIONS
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            # 잠금을 기다리는 동안 계정 접근이 해제됐을 수 있어 세션과 권한을 다시 확인합니다.
            digest = hashlib.sha256(request.cookies.get("moa_session", "").encode()).hexdigest()
            user = db.execute(
                (
                    "SELECT u.id,u.username,u.role FROM users u JOIN sessions s ON s.user_id=u.id WHERE "
                    "u.id=? AND u.active=1 AND s.digest=? AND s.expires>?"
                ),
                (g.user["id"], digest, int(time.time())),
            ).fetchone()
            if not user:
                db.rollback()
                db.close()
                return jsonify(error="계정 접근이 해제되었습니다."), 401
            g.user = dict(user)
            if endpoint in ("member_create", "member_update") and user["role"] != "owner":
                db.rollback()
                db.close()
                return jsonify(error="소유자만 공동관리자 계정을 관리할 수 있습니다."), 403
            g.account_revision = endpoint in ACCOUNT_ENDPOINTS
            if g.account_revision:
                revision = db.execute(
                    "SELECT revision FROM workspace_revision WHERE id=1"
                ).fetchone()[0]
            else:
                row = db.execute(
                    "SELECT revision FROM personal_revisions WHERE user_id=?", (g.user["id"],)
                ).fetchone()
                revision = row[0] if row else 0
            g.request_revision = revision
            if write:
                supplied = request.headers.get("If-Match")
                shared = (
                    db.execute("SELECT 1 FROM users WHERE role='manager' AND active=1").fetchone()
                    is not None
                )
                if supplied is None and shared:
                    db.rollback()
                    db.close()
                    return (
                        jsonify(
                            error="공동 관리가 활성화되었습니다. 최신 화면을 다시 불러온 뒤 저장해 주세요.",
                            code="revision_required",
                        ),
                        428,
                    )
                if supplied is not None and supplied != str(revision):
                    db.rollback()
                    db.close()
                    return (
                        jsonify(
                            error="다른 화면에서 먼저 변경했습니다. 입력 내용을 확인하고 최신 자료를 다시 불러온 뒤 저장해 주세요.",
                            code="revision_conflict",
                        ),
                        409,
                    )
            g.shared_raw = db
            g.shared_db = RequestConnection(db)
        except Exception:
            db.close()
            raise

    @app.after_request
    def finish_shared(response):
        """성공이면 실제 변경·버전·이력을 함께 확정하고, 실패면 모두 되돌립니다."""
        db = getattr(g, "shared_raw", None)
        if db is None:
            return response
        try:
            if response.status_code < 400:
                if request.endpoint in MUTATIONS:
                    if not g.account_revision:
                        # 카드 등록·변경과 계좌 쪽 중복 보류를 같은 저장 단위로 확정합니다.
                        bank_expenses.reconcile(db, g.user["id"])
                    if g.account_revision:
                        db.execute("UPDATE workspace_revision SET revision=revision+1 WHERE id=1")
                    else:
                        db.execute(
                            "INSERT INTO personal_revisions(user_id,revision) VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1",
                            (g.user["id"],),
                        )
                    # 요청 본문·암호·파일명·메모·가맹점 내용은 이력에 복제하지 않습니다.
                    target = getattr(g, "audit_target", "") or "/".join(
                        str(value)
                        for value in (request.view_args or {}).values()
                        if value is not None
                    )
                    if not target:
                        body = response.get_json(silent=True) or {}
                        target = str(body["id"]) if "id" in body else "공동 가계부"
                    db.execute(
                        "INSERT INTO audit_log(actor_id,actor_name,action,target,data_owner_id) VALUES(?,?,?,?,?)",
                        (
                            g.user["id"],
                            g.user["username"],
                            MUTATIONS[request.endpoint],
                            target,
                            (
                                None
                                if request.endpoint in {"member_create", "member_update"}
                                else g.user["id"]
                            ),
                        ),
                    )
                    g.request_revision += 1
                db.commit()
                response.headers["X-Moa-Revision"] = str(g.request_revision)
            else:
                db.rollback()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
            g.shared_raw = None
            g.shared_db = None
        return response

    @app.teardown_request
    def close_shared(error):
        """처리 중 예외가 발생해도 끝나지 않은 저장 단위와 연결이 남지 않게 정리합니다."""
        db = getattr(g, "shared_raw", None)
        if db is not None:
            db.rollback()
            db.close()
            g.shared_raw = None
            g.shared_db = None

    @app.get("/api/members")
    def members():
        """소유자는 전체 계정을, 공동관리자는 본인 계정의 공개 정보만 확인합니다."""
        db = connect()
        try:
            rows = [
                dict(row)
                for row in db.execute("SELECT id,username,role,active FROM users ORDER BY id")
            ]
            return jsonify(
                user=g.user,
                members=(
                    rows
                    if g.user["role"] == "owner"
                    else [row for row in rows if row["id"] == g.user["id"]]
                ),
            )
        finally:
            db.close()

    @app.post("/api/members")
    def member_create():
        """소유자가 공동관리자 계정을 만듭니다. 활성 공동관리자는 한 명까지만 허용합니다."""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="계정 정보를 입력해 주세요."), 400
        name = payload.get("username")
        password = payload.get("password")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,39}", name):
            return (
                jsonify(error="아이디는 영문 소문자·숫자·밑줄·하이픈 3~40자로 입력해 주세요."),
                400,
            )
        if not valid_password(password):
            return jsonify(error="비밀번호는 12~128자로 입력해 주세요."), 400
        db = connect()
        if db.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
            return jsonify(error="이미 사용한 아이디입니다."), 409
        if db.execute("SELECT 1 FROM users WHERE role='manager' AND active=1").fetchone():
            return jsonify(error="공동관리자는 1명까지 활성화할 수 있습니다."), 400
        cursor = db.execute(
            "INSERT INTO users(username,password_hash,role) VALUES(?,?,'manager')",
            (name, generate_password_hash(password)),
        )
        return jsonify(ok=True, id=cursor.lastrowid)

    @app.put("/api/members/<int:user_id>")
    def member_update(user_id):
        """소유자가 공동관리자의 접근·암호를 변경하고 해당 계정의 기존 세션을 폐기합니다."""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not payload or set(payload) - {"active", "password"}:
            return jsonify(error="접근 여부 또는 새 비밀번호를 입력해 주세요."), 400
        if "active" in payload and type(payload["active"]) is not bool:
            return jsonify(error="접근 여부를 확인해 주세요."), 400
        if "password" in payload and not valid_password(payload["password"]):
            return jsonify(error="비밀번호는 12~128자로 입력해 주세요."), 400
        db = connect()
        row = db.execute("SELECT * FROM users WHERE id=? AND role='manager'", (user_id,)).fetchone()
        if not row:
            return jsonify(error="공동관리자 계정을 찾지 못했습니다."), 404
        if (
            payload.get("active")
            and db.execute(
                "SELECT 1 FROM users WHERE role='manager' AND active=1 AND id!=?", (user_id,)
            ).fetchone()
        ):
            return jsonify(error="활성 공동관리자는 1명까지 가능합니다."), 400
        if "active" in payload:
            db.execute("UPDATE users SET active=? WHERE id=?", (int(payload["active"]), user_id))
        if "password" in payload:
            db.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (generate_password_hash(payload["password"]), user_id),
            )
        # 계정 해제·암호 초기화 뒤 예전 단축어 키가 다시 작동하지 않게 폐기합니다.
        db.execute("DELETE FROM sms_keys WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return jsonify(ok=True)

    @app.put("/api/account/password")
    def own_password():
        """현재 비밀번호 확인 후 본인 암호를 바꾸고 본인 계정의 모든 세션을 종료합니다."""
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or not valid_password(payload.get("password"))
            or not isinstance(payload.get("current_password"), str)
            or len(payload["current_password"]) > 128
        ):
            return jsonify(error="현재 비밀번호와 새 비밀번호(12~128자)를 확인해 주세요."), 400
        db = connect()
        row = db.execute("SELECT password_hash FROM users WHERE id=?", (g.user["id"],)).fetchone()
        if not check_password_hash(row["password_hash"], payload["current_password"]):
            return jsonify(error="현재 비밀번호가 맞지 않습니다."), 400
        hashed = generate_password_hash(payload["password"])
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, g.user["id"]))
        if g.user["role"] == "owner":
            db.execute("UPDATE owner SET password_hash=? WHERE id=1", (hashed,))
        db.execute("DELETE FROM sms_keys WHERE user_id=?", (g.user["id"],))
        db.execute("DELETE FROM sessions WHERE user_id=?", (g.user["id"],))
        return jsonify(ok=True)

    @app.get("/api/audit")
    def audit_history():
        """본문이나 비밀번호 없이 최근 변경 작업자·종류·대상·시각을 반환합니다."""
        db = connect()
        return jsonify(
            items=[
                dict(row)
                for row in db.execute(
                    "SELECT id,actor_name,action,target,created FROM audit_log WHERE data_owner_id=? OR data_owner_id IS NULL ORDER BY id DESC LIMIT 100",
                    (current_user_id(),),
                )
            ]
        )


def valid_password(value):
    """앱 로그인 비밀번호의 자료형과 길이가 허용 범위인지 확인합니다."""
    return isinstance(value, str) and 12 <= len(value) <= 128
