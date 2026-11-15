"""Gmail의 MOA 라벨에서 명세서 첨부 목록을 수집하고 검토 상태를 관리합니다.

IMAP 연결은 메일을 읽기만 합니다. 원본 첨부는 Gmail에 두고 필요할 때 메모리로 가져오며,
거래 저장은 사용자 검토 뒤에만 진행합니다. 암호는 NAS 비밀 파일에서 읽습니다.
실제 주기 실행은 mail_worker.py, 화면 요청을 받는 API는 app.py에 있습니다.
"""

import email.policy
import hashlib
import imaplib
import json
import os
import re
import ssl
import sqlite3
import time
import uuid
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path
from contextlib import contextmanager
from personal_data import current_user_id

MAX_MESSAGE = 16 * 1024 * 1024
MAX_ATTACHMENT = 8 * 1024 * 1024
EXTENSIONS = {".html", ".htm", ".pdf", ".xlsx", ".csv"}
BRANDS = {
    "삼성": ("삼성", "samsungcard"),
    "현대": ("현대", "hyundaicard"),
    "신한": ("신한", "shinhancard"),
    "KB국민": ("국민", "kbcard"),
    "우리": ("우리카드", "wooricard"),
    "롯데": ("롯데", "lottecard"),
}


class MailError(ValueError):
    pass


# 1. Gmail 연결과 비밀 파일 설정
# ─────────────────────────────────────────────────────────────────
def personal_mail_settings():
    """소유자의 기존 설정은 유지하고 다른 사용자는 전용 폴더만 읽습니다. 폴백하지 않습니다."""
    user_id = current_user_id()
    if user_id == 1:
        return dict(
            user=os.environ.get("MOA_GMAIL_USER", "").strip(),
            folder=os.environ.get("MOA_GMAIL_FOLDER", "MOA"),
            password_file=Path(
                os.environ.get("MOA_GMAIL_PASSWORD_FILE", "/run/moa-secrets/gmail-app-password")
            ),
            statement_file=Path(
                os.environ.get("MOA_STATEMENT_PASSWORD_FILE", "/run/moa-secrets/statement-password")
            ),
            online=os.environ.get("MOA_MAIL_SAMSUNG_ONLINE") == "1",
        )
    root = Path(os.environ.get("MOA_PERSONAL_SECRETS_DIR", "/run/moa-secrets/users")) / str(user_id)
    settings = {}
    path = root / "gmail.json"
    if path.is_file():
        try:
            if path.stat().st_size > 4096:
                raise ValueError()
            settings = json.loads(path.read_text())
            if (
                not isinstance(settings, dict)
                or not isinstance(settings.get("user"), str)
                or not isinstance(settings.get("folder", "MOA"), str)
                or type(settings.get("samsung_online", False)) is not bool
            ):
                raise ValueError()
        except (ValueError, OSError, UnicodeError):
            raise MailError("본인 Gmail 설정 파일을 확인해 주세요.") from None
    return dict(
        user=settings.get("user", "").strip(),
        folder=settings.get("folder", "MOA"),
        password_file=root / "gmail-app-password",
        statement_file=root / "statement-password",
        online=settings.get("samsung_online", False),
    )


def config():
    """환경 변수와 NAS 비밀 파일을 검사해 Gmail 연결 설정을 읽습니다."""
    settings = personal_mail_settings()
    user, folder, secret = settings["user"], settings["folder"], settings["password_file"]
    if not user or not secret.is_file():
        raise MailError("Gmail 연결 설정이 필요합니다.")
    if not re.fullmatch(r"[A-Za-z0-9_./ -]{1,80}", folder):
        raise MailError("메일 라벨은 MOA처럼 영문 이름을 사용해 주세요.")
    try:
        password = secret.read_text().strip().replace(" ", "")
    except (OSError, UnicodeError):
        raise MailError("Gmail 앱 비밀번호 파일을 읽지 못했습니다.") from None
    if not re.fullmatch(r"[A-Za-z0-9]{16}", password):
        raise MailError("Gmail 앱 비밀번호 파일을 확인해 주세요.")
    namespace = "" if current_user_id() == 1 else str(current_user_id()) + "\0"
    key = hashlib.sha256((namespace + user.lower() + "\0" + folder).encode()).hexdigest()
    return user, folder, password, key


def schema(db):
    """메일 후보·수집 위치·처리 상태·즉시 수집 요청 대기열을 보관할 테이블을 준비합니다."""
    db.executescript(
        """CREATE TABLE IF NOT EXISTS mail_candidates(
        id TEXT PRIMARY KEY, mailbox TEXT NOT NULL, validity TEXT NOT NULL, uid INTEGER NOT NULL,
        part INTEGER NOT NULL, filename TEXT NOT NULL, card TEXT NOT NULL, size INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'waiting', auto_count INTEGER, auto_error TEXT, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(mailbox,validity,uid,part));
        CREATE TABLE IF NOT EXISTS mail_cursors(mailbox TEXT NOT NULL,validity TEXT NOT NULL,last_uid INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(mailbox,validity));
        CREATE TABLE IF NOT EXISTS mail_status(mailbox TEXT PRIMARY KEY,checked TEXT,error TEXT,found INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS mail_control(mailbox TEXT PRIMARY KEY,requested INTEGER NOT NULL DEFAULT 0,completed INTEGER NOT NULL DEFAULT 0,requested_at INTEGER NOT NULL DEFAULT 0,heartbeat INTEGER NOT NULL DEFAULT 0,running INTEGER NOT NULL DEFAULT 0,remaining INTEGER NOT NULL DEFAULT 0,error TEXT);"""
    )

    if "owner_id" not in {row[1] for row in db.execute("PRAGMA table_info(mail_candidates)")}:
        try:
            db.execute("ALTER TABLE mail_candidates ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            if "owner_id" not in {
                row[1] for row in db.execute("PRAGMA table_info(mail_candidates)")
            }:
                raise

    columns = {row[1] for row in db.execute("PRAGMA table_info(mail_candidates)")}
    if "digest" not in columns:
        try:
            db.execute("ALTER TABLE mail_candidates ADD COLUMN digest TEXT")
        except sqlite3.OperationalError:
            if "digest" not in {row[1] for row in db.execute("PRAGMA table_info(mail_candidates)")}:
                raise


def mark_read(connect, digest):
    """원본 해시가 같은 수집 후보에 열람 상태를 표시합니다. Gmail의 읽음 상태를 바꾸지는 않습니다."""
    db = connect()
    schema(db)
    try:
        with db:
            db.execute(
                (
                    "UPDATE mail_candidates SET state='prepared',auto_error=NULL WHERE digest=? AND state"
                    " NOT IN ('stale','dismissed') AND owner_id=?"
                ),
                (digest, current_user_id()),
            )
    finally:
        db.close()


def mark_failed(connect, digest):
    """첨부 해시에 맞는 후보의 판독 실패 상태를 기록합니다."""
    db = connect()
    schema(db)
    try:
        with db:
            db.execute(
                "UPDATE mail_candidates SET auto_error=? WHERE digest=? AND state NOT IN ('stale','dismissed') AND owner_id=?",
                (
                    "내역을 읽지 못했습니다. 다시 검토하여 비밀번호와 파일 형식을 확인해 주세요.",
                    digest,
                    current_user_id(),
                ),
            )
    finally:
        db.close()


def common_password():
    """NAS 비밀 파일의 공통 명세서 암호를 읽습니다. 화면에 반환하는 값이 아닙니다."""
    path = personal_mail_settings()["statement_file"]
    if not path.is_file():
        return None
    if path.stat().st_size > 1024:
        raise MailError("공통 명세서 비밀번호 파일을 확인해 주세요.")
    try:
        value = path.read_text().rstrip("\r\n")
    except (OSError, UnicodeError):
        raise MailError("공통 명세서 비밀번호 파일을 읽지 못했습니다.") from None
    if not value or len(value) > 128:
        raise MailError("공통 명세서 비밀번호는 1~128자로 설정해 주세요.")
    return value


def samsung_online():
    """이메일 명세서 판독 중 삼성 공식 사이트 연결이 허용됐는지 확인합니다."""
    return personal_mail_settings()["online"]


# 2. 발신 인증과 카드사별 자동 판독
# ─────────────────────────────────────────────────────────────────
def automatic_check(item, raw):
    """공식 발신 도메인과 Gmail 발신 인증이 맞는 첨부만 공통 암호로 자동 열기를 시도합니다."""
    try:
        password = common_password()
    except (MailError, OSError):
        return None, "공통 명세서 비밀번호 파일 설정을 확인해 주세요."
    if password is None:
        return None, None
    extension = Path(item["filename"]).suffix.lower()
    if extension not in {".html", ".htm", ".pdf"}:
        return None, None
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    sender = parseaddr(str(message.get("From", "")))[1].rsplit("@", 1)[-1].lower()
    domains = {
        "삼성": ("samsungcard.com",),
        "현대": ("hyundaicard.com",),
        "우리": ("wooricard.com",),
        "신한": ("shinhancard.com",),
        "롯데": ("lottecard.co.kr", "lottecard.com"),
    }
    official = any(
        sender == domain or sender.endswith("." + domain)
        for domain in domains.get(item["card"], ())
    )
    auth = str(message.get("Authentication-Results", "")).lower()
    aligned = re.search(
        r"dmarc=pass\b[^;]*?header[.]from=" + re.escape(sender) + r"(?:[;\s]|$)", auth
    )
    if not official or not auth.startswith("mx.google.com;") or not aligned:
        return None, "발신 인증을 확인하지 못해 자동 열기를 보류했습니다. 검토 시작에서 확인하세요."
    try:
        tables = open_statement(item, password)
        from secure_html import normalize_statement_tables

        tables = normalize_statement_tables(tables, item["card"])
        return sum(max(0, len(table) - 1) for table in tables), None
    except Exception:
        return None, "자동 열기에 실패했습니다. 검토 시작에서 오류와 비밀번호 설정을 확인하세요."


def open_statement(item, password):
    """카드사와 확장자에 맞는 보안 HTML 또는 PDF 판독기를 고릅니다."""
    extension = Path(item["filename"]).suffix.lower()
    card = item["card"]
    if extension == ".pdf" and card == "신한":
        from secure_pdf import unlock_pdf

        return unlock_pdf(item["data"], password)
    if extension in {".html", ".htm"} and card in {"삼성", "현대", "롯데", "우리"}:
        from secure_html import unlock_statement

        return unlock_statement(
            item["data"], password, card=card, allow_online=card == "삼성" and samsung_online()
        )
    raise MailError("이 카드사의 첨부파일 형식은 수동 파일 업로드로 확인해 주세요.")


# 3. 메일 구조 해석과 읽기 전용 IMAP 통신
# ─────────────────────────────────────────────────────────────────
def parts(raw):
    """메일 원문을 해석해 지원하는 첨부파일만 크기 제한 안에서 추립니다."""
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    context = str(message.get("From", "")) + " " + str(message.get("Subject", ""))
    result = []
    for index, part in enumerate(message.walk()):
        name = part.get_filename()
        if part.is_multipart() or not name:
            continue
        name = re.sub(r"[\x00-\x1f\x7f]", "", Path(name.replace("\\", "/")).name).strip()[:240]
        if Path(name).suffix.lower() not in EXTENSIONS:
            continue
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_ATTACHMENT:
            continue
        text = (context + " " + name).lower()
        matches = [card for card, words in BRANDS.items() if any(word in text for word in words)]
        # A sender/filename hint is not proof of issuer identity.
        result.append(
            dict(
                part=index,
                filename=name,
                size=len(data),
                card=matches[0] if len(matches) == 1 else "",
                data=data,
            )
        )
    return result


@contextmanager
def session():
    """Gmail IMAP 연결을 열고 읽기 전용 폴더를 선택한 뒤 사용 후 닫습니다."""
    client = None
    try:
        user, folder, password, key = config()
        client = imaplib.IMAP4_SSL(
            "imap.gmail.com", 993, ssl_context=ssl.create_default_context(), timeout=8
        )
        client.login(user, password)
        password = ""
        status, _ = client.select('"' + folder.replace('"', "") + '"', readonly=True)
        if status != "OK":
            raise MailError("설정한 Gmail 라벨과 IMAP 표시 설정을 확인해 주세요.")
        response = client.response("UIDVALIDITY")[1]
        validity = response[0].decode() if response and response[0] else ""
        if not validity.isdigit():
            raise MailError("메일함 식별 정보를 확인하지 못했습니다.")
        yield client, key, validity
    except MailError:
        raise
    except imaplib.IMAP4.error:
        raise MailError(
            "Gmail 인증 또는 라벨 연결에 실패했습니다. 앱 비밀번호와 IMAP 설정을 확인해 주세요."
        ) from None
    except Exception:
        raise MailError(
            "Gmail에 연결하지 못했습니다. NAS 인터넷 연결과 설정을 확인해 주세요."
        ) from None
    finally:
        if client:
            try:
                client.logout()
            except Exception:
                pass


def fetch_message(client, uid):
    """UID로 지정한 메일을 읽음 표시 없이 가져옵니다. UID는 Gmail 안에서 쓰는 메일 식별자입니다."""
    status, items = client.uid("fetch", str(uid), "(RFC822.SIZE)")
    sizes = [
        int(value)
        for item in items or []
        if isinstance(item, bytes)
        for value in re.findall(rb"RFC822.SIZE (\d+)", item)
    ]
    if status != "OK" or not sizes:
        raise MailError("메일 크기를 확인하지 못했습니다.")
    if max(sizes) > MAX_MESSAGE:
        return None
    status, items = client.uid("fetch", str(uid), "(BODY.PEEK[])")
    if status != "OK":
        raise MailError("메일을 읽지 못했습니다.")
    values = [
        item[1]
        for item in items or []
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes)
    ]
    if len(values) != 1 or len(values[0]) > MAX_MESSAGE:
        raise MailError("지원하는 메일 크기를 초과했습니다.")
    return values[0]


# 4. 새 메일 수집과 상태 기록
# ─────────────────────────────────────────────────────────────────
def sync(connect):
    """새 메일을 제한된 묶음으로 수집하고 첨부 메타데이터·마지막 처리 위치를 저장합니다."""
    _, _, _, mailbox = config()
    db = connect()
    schema(db)
    try:
        with session() as (client, key, validity):
            with db:
                db.execute(
                    "UPDATE mail_candidates SET state='stale' WHERE mailbox=? AND validity!=?",
                    (key, validity),
                )
            cursor = db.execute(
                "SELECT last_uid FROM mail_cursors WHERE mailbox=? AND validity=?", (key, validity)
            ).fetchone()
            last = cursor[0] if cursor else 0
            status, items = client.uid("search", None, "ALL")
            if status != "OK":
                raise MailError("메일 목록을 읽지 못했습니다.")
            available = sorted(
                {
                    int(uid)
                    for line in items or []
                    if isinstance(line, bytes)
                    for uid in line.split()
                    if uid.isdigit() and int(uid) > last
                }
            )
            uids = available[:10]
            found = 0
            for index, uid in enumerate(uids):
                with db:
                    db.execute(
                        "UPDATE mail_control SET heartbeat=?,remaining=? WHERE mailbox=?",
                        (int(time.time()), len(available) - index, mailbox),
                    )
                raw = fetch_message(client, uid)
                attachments = parts(raw) if raw else []
                checks = []
                for item in attachments:
                    with db:
                        db.execute(
                            "UPDATE mail_control SET heartbeat=? WHERE mailbox=?",
                            (int(time.time()), mailbox),
                        )
                    checks.append(automatic_check(item, raw))
                with db:
                    for item, (count, error) in zip(attachments, checks):
                        result = db.execute(
                            (
                                "INSERT OR IGNORE INTO "
                                "mail_candidates(id,mailbox,validity,uid,part,filename,card,size,state,auto_count,auto_error,owner_id)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                            ),
                            (
                                uuid.uuid4().hex,
                                key,
                                validity,
                                uid,
                                item["part"],
                                item["filename"],
                                item["card"],
                                item["size"],
                                "verified" if count is not None else "waiting",
                                count,
                                error,
                                current_user_id(),
                            ),
                        )
                        found += result.rowcount
                        db.execute(
                            "UPDATE mail_candidates SET digest=? WHERE mailbox=? AND validity=? AND uid=? AND part=?",
                            (
                                hashlib.sha256(item["data"]).hexdigest(),
                                key,
                                validity,
                                uid,
                                item["part"],
                            ),
                        )
                    db.execute(
                        "INSERT INTO mail_cursors VALUES(?,?,?) ON CONFLICT(mailbox,validity) DO UPDATE SET last_uid=excluded.last_uid",
                        (key, validity, uid),
                    )
            with db:
                db.execute(
                    "UPDATE mail_control SET remaining=? WHERE mailbox=?",
                    (len(available) - len(uids), mailbox),
                )
            with db:
                db.execute(
                    (
                        "INSERT INTO mail_status VALUES(?,CURRENT_TIMESTAMP,NULL,?) ON CONFLICT(mailbox) DO "
                        "UPDATE SET checked=CURRENT_TIMESTAMP,error=NULL,found=excluded.found"
                    ),
                    (key, found),
                )
            return found
    except MailError as error:
        with db:
            db.execute(
                (
                    "INSERT INTO mail_status VALUES(?,CURRENT_TIMESTAMP,?,0) ON CONFLICT(mailbox) DO "
                    "UPDATE SET checked=CURRENT_TIMESTAMP,error=excluded.error"
                ),
                (mailbox, str(error)),
            )
        raise
    finally:
        db.close()


# 5. 화면용 목록과 원본 첨부 조회
# ─────────────────────────────────────────────────────────────────
def inbox(connect):
    """현재 연결 설정과 저장된 첨부 후보를 화면용 처리 현황으로 조합합니다."""
    try:
        _, folder, _, key = config()
    except (MailError, OSError) as error:
        return dict(
            configured=False,
            items=[],
            auto_password=False,
            error=(
                str(error) if isinstance(error, MailError) else "Gmail 설정 파일을 읽지 못했습니다."
            ),
        )
    db = connect()
    schema(db)
    try:
        items = [
            dict(row)
            for row in db.execute(
                (
                    "SELECT id,filename,card,size,state,auto_count,auto_error,created,digest FROM "
                    "mail_candidates WHERE mailbox=? AND owner_id=? AND state NOT IN ('dismissed','stale') ORDER BY uid "
                    "DESC,part"
                ),
                (key, current_user_id()),
            )
        ]
        saved = {}
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='imports'"
        ).fetchone():
            for row in db.execute(
                (
                    "SELECT i.digest,i.created,COUNT(t.id) AS count FROM imports i LEFT JOIN transactions"
                    " t ON t.import_id=i.id WHERE i.owner_id=? GROUP BY i.id"
                ),
                (current_user_id(),),
            ):
                digest = row["digest"].split(":", 1)[0]
                entry = saved.setdefault(digest, dict(saved_tables=0, saved_count=0, saved_at=None))
                entry["saved_tables"] += 1
                entry["saved_count"] += row["count"]
                entry["saved_at"] = max(entry["saved_at"] or "", row["created"])
        summary = dict(received=0, needs_review=0, ready=0, saved=0)
        for item in items:
            item.update(
                saved.get(item.pop("digest"), dict(saved_tables=0, saved_count=0, saved_at=None))
            )
            item["stage"] = (
                "saved"
                if item["saved_tables"]
                else (
                    "needs_review"
                    if item["auto_error"]
                    else "ready" if item["state"] in ("verified", "prepared") else "received"
                )
            )
            summary[item["stage"]] += 1
        total = len(items)
        items = items[:100]
        refresh = db.execute(
            "SELECT requested,completed,requested_at,heartbeat,running,remaining,error FROM mail_control WHERE mailbox=?",
            (key,),
        ).fetchone()
        refresh = dict(refresh) if refresh else None
        if refresh:
            refresh["worker_online"] = bool(
                refresh["heartbeat"] and time.time() - refresh["heartbeat"] < 120
            )
            refresh["unresponsive"] = bool(
                (refresh["running"] or refresh["requested"] > refresh["completed"])
                and not refresh["worker_online"]
                and time.time() - refresh["requested_at"] > 15
            )
        status = db.execute(
            "SELECT checked,error,found FROM mail_status WHERE mailbox=?", (key,)
        ).fetchone()
        try:
            auto_password = common_password() is not None
            password_error = None
        except (MailError, OSError):
            auto_password = False
            password_error = "공통 명세서 비밀번호 파일 설정을 확인해 주세요."
        return dict(
            configured=True,
            password_error=password_error,
            folder=folder,
            items=items,
            auto_password=auto_password,
            samsung_online=samsung_online(),
            status=dict(status) if status else None,
            summary=summary,
            total=total,
            refresh=refresh,
        )
    finally:
        db.close()


def attachment(connect, identifier):
    """후보가 가리키는 원본 첨부를 Gmail에서 다시 읽어 메모리로 반환합니다."""
    _, _, _, key = config()
    db = connect()
    schema(db)
    try:
        row = db.execute(
            "SELECT * FROM mail_candidates WHERE id=? AND mailbox=? AND owner_id=? AND state NOT IN ('dismissed','stale')",
            (identifier, key, current_user_id()),
        ).fetchone()
    finally:
        db.close()
    if not row:
        raise MailError("메일 첨부 기록을 찾지 못했습니다.")
    with session() as (client, mailbox, validity):
        if mailbox != row["mailbox"] or validity != row["validity"]:
            raise MailError("메일함이 변경되었습니다. 새로 수집된 목록을 확인해 주세요.")
        raw = fetch_message(client, row["uid"])
        for item in parts(raw) if raw else []:
            if item["part"] == row["part"] and item["filename"] == row["filename"]:
                db = connect()
                try:
                    with db:
                        db.execute(
                            (
                                "UPDATE mail_candidates SET state=CASE WHEN state IN ('verified','prepared') THEN "
                                "state ELSE 'opened' END,digest=? WHERE id=?"
                            ),
                            (hashlib.sha256(item["data"]).hexdigest(), identifier),
                        )
                finally:
                    db.close()
                return item
    raise MailError("원본 첨부파일을 찾지 못했습니다. Gmail에서 삭제되었는지 확인해 주세요.")


def mark_prepared(connect, identifier, card, tables):
    """검토 화면 준비가 끝난 후보의 카드사·추출 건수·상태를 갱신합니다."""
    from secure_html import normalize_statement_tables

    tables = normalize_statement_tables(tables, card)
    db = connect()
    schema(db)
    try:
        with db:
            db.execute(
                "UPDATE mail_candidates SET state='prepared',card=?,auto_count=?,auto_error=NULL WHERE id=? AND owner_id=?",
                (card, sum(max(0, len(t) - 1) for t in tables), identifier, current_user_id()),
            )
    finally:
        db.close()


# 6. 즉시 새로고침 요청 대기열
# ─────────────────────────────────────────────────────────────────
def request_refresh(connect):
    """화면의 새로고침 요청을 DB 대기열에 기록합니다. 여기서는 Gmail 통신을 하지 않습니다."""
    _, _, _, key = config()
    db = connect()
    schema(db)
    try:
        with db:
            db.execute("INSERT OR IGNORE INTO mail_control(mailbox) VALUES(?)", (key,))
            db.execute(
                (
                    "UPDATE mail_control SET requested=requested+1,requested_at=?,error=NULL WHERE "
                    "mailbox=? AND requested<=completed"
                ),
                (int(time.time()), key),
            )
            ticket = db.execute(
                "SELECT requested FROM mail_control WHERE mailbox=?", (key,)
            ).fetchone()[0]
        return ticket
    finally:
        db.close()


def process_refresh(connect, *, automatic=False):
    """수집 프로세스가 대기 요청 또는 주기 실행을 처리하고 진행·완료 상태를 기록합니다."""
    _, _, _, key = config()
    db = connect()
    schema(db)
    try:
        with db:
            db.execute("INSERT OR IGNORE INTO mail_control(mailbox) VALUES(?)", (key,))
            db.execute(
                "UPDATE mail_control SET heartbeat=? WHERE mailbox=? AND heartbeat<?",
                (int(time.time()), key, int(time.time()) - 5),
            )
            state = db.execute(
                "SELECT requested,completed FROM mail_control WHERE mailbox=?", (key,)
            ).fetchone()
            manual = state["requested"] > state["completed"]
            ticket = state["requested"]
            if not manual and not automatic:
                return None
            db.execute(
                "UPDATE mail_control SET running=1,error=NULL,heartbeat=? WHERE mailbox=?",
                (int(time.time()), key),
            )
        found = 0
        error = None
        try:
            # A click drains up to 100 new messages without five-minute gaps.
            for _ in range(10 if manual else 1):
                found += sync(connect)
                remaining = db.execute(
                    "SELECT remaining FROM mail_control WHERE mailbox=?", (key,)
                ).fetchone()[0]
                if not remaining:
                    break
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, MailError)
                else "Gmail 수집에 실패했습니다. NAS 연결 상태를 확인해 주세요."
            )
        with db:
            db.execute(
                "UPDATE mail_control SET completed=MAX(completed,?),running=0,heartbeat=?,error=? WHERE mailbox=?",
                (ticket, int(time.time()), error, key),
            )
        return dict(ok=error is None, found=found)
    finally:
        db.close()
