"""서버 관리 터미널에서 개인 Gmail 설정을 준비합니다.

비밀번호는 getpass로 받고 명령 인자·출력에 넣지 않습니다. 웹 API에서 실행하는 도구가 아닙니다.
설정 대상은 등록된 공동관리자의 숫자 ID이며, 소유자(1번)는 기존 환경 설정을 유지합니다.
"""

import getpass
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile


def write_private_file(path, content):
    """완성한 파일을 원자적으로 교체하고 앱 사용자만 읽을 수 있게 합니다."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, encoding="utf-8", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.chmod(0o600)
        os.chown(temporary, 1000, 1000)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def save_configuration(root, user_id, user, folder, password, statement="", online=False):
    """내용을 먼저 검증하고 개인 폴더에만 저장합니다. 빈 명세서 암호는 기존 값을 유지합니다."""
    if type(user_id) is not int or user_id < 2:
        raise ValueError("공동관리자의 개인 설정 번호를 확인해 주세요.")
    if not isinstance(user, str) or len(user) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+", user):
        raise ValueError("Gmail 주소를 확인해 주세요.")
    if not isinstance(folder, str) or not re.fullmatch(r"[A-Za-z0-9_./ -]{1,80}", folder):
        raise ValueError("라벨 이름은 MOA처럼 영문으로 입력해 주세요.")
    password = password.replace(" ", "")
    if not re.fullmatch(r"[A-Za-z0-9]{16}", password):
        raise ValueError("Gmail 앱 비밀번호는 공백을 제외한 16자리여야 합니다.")
    if len(statement) > 128 or type(online) is not bool:
        raise ValueError("명세서 암호 또는 연결 설정을 확인해 주세요.")
    root = Path(root)
    for directory in (root / "users", root / "users" / str(user_id)):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        os.chown(directory, 1000, 1000)
    destination = root / "users" / str(user_id)
    write_private_file(destination / "gmail-app-password", password)
    if statement:
        write_private_file(destination / "statement-password", statement)
    write_private_file(
        destination / "gmail.json",
        json.dumps(dict(user=user, folder=folder, samsung_online=online), ensure_ascii=False),
    )


def main():
    """활성 계정을 확인한 뒤 화면에 가려진 비밀번호 입력으로 설정합니다."""
    user_id = int(input("공동관리자의 개인 설정 번호: "))
    database = Path(os.environ.get("CARD_DB", "/data/cards.sqlite3"))
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT username FROM users WHERE id=? AND role='manager' AND active=1", (user_id,)
        ).fetchone()
    if not row:
        raise ValueError("활성 공동관리자 계정을 먼저 만들어 주세요.")
    print("설정할 계정:", row[0])
    user = input("개인 Gmail 주소: ").strip()
    folder = input("명세서 라벨 [MOA]: ").strip() or "MOA"
    password = getpass.getpass("Gmail 앱 비밀번호: ")
    statement = getpass.getpass("개인 명세서 공통 암호 (비우면 기존 값 유지): ")
    online = input("삼성 공식 사이트 자동 연결 허용? [y/N]: ").strip().lower() == "y"
    save_configuration("/setup-secrets", user_id, user, folder, password, statement, online)
    password = statement = ""
    print("개인 Gmail 설정을 저장했습니다. 앱의 이메일 화면에서 목록 새로고침을 누르세요.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error):
        raise SystemExit(
            "설정하지 못했습니다. 계정 번호·입력값·비밀 볼륨 쓰기 권한을 확인해 주세요."
        ) from None
