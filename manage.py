"""서버 터미널에서 쓰는 관리 명령입니다.

- `python manage.py`      소유자 비밀번호를 처음 설정하거나 복구합니다.
                          getpass로 입력을 숨기고, 비밀번호를 명령 인자로 받지 않습니다.
                          실행하면 기존 로그인 세션은 모두 종료되며 카드 내역은 유지됩니다.
- `python manage.py demo` 빈 DB에 가상 카드 명세서 3개를 넣어 화면을 둘러볼 수 있게 합니다
                          (demo_data.py). 거래가 있는 DB에는 넣지 않고 멈춥니다.
"""

import getpass
import sys


def set_owner_password():
    """숨긴 입력으로 소유자 비밀번호를 정합니다."""
    from app import connect
    from auth import set_password

    password = getpass.getpass("모아 로그인 비밀번호 (12~128자): ")
    confirm = getpass.getpass("비밀번호 확인: ")
    if password != confirm:
        raise SystemExit("비밀번호가 일치하지 않습니다. 변경하지 않았습니다.")
    try:
        set_password(connect, password)
    except ValueError as exc:
        raise SystemExit(str(exc))
    finally:
        password = confirm = ""
    print("비밀번호를 설정했습니다. 기존 로그인 세션은 모두 종료되었습니다.")


def fill_demo():
    """가상 데이터를 넣고 데모 로그인 비밀번호를 알려 줍니다."""
    import demo_data

    for card, count in demo_data.fill():
        print(f"{card}: 가상 거래 {count}건 저장")
    print(f"데모 로그인 비밀번호: {demo_data.DEMO_PASSWORD}  (가상 데이터 전용입니다)")


if __name__ == "__main__":
    command = sys.argv[1:] or [""]
    if command == ["demo"]:
        fill_demo()
    elif command == [""]:
        set_owner_password()
    else:
        raise SystemExit("사용법: python manage.py [demo]")
