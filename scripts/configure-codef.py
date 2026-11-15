"""서버 터미널에서 CODEF 데모 키를 가려 입력받고 비밀 파일에 원자적으로 저장합니다.

키는 명령 인자·출력에 넣지 않고 항목별로 검증합니다. 형식 오류는 해당 항목만 다시 입력받고,
저장 오류는 비밀 값을 포함하지 않은 단계·원인으로 안내합니다. 외부 통신은 하지 않습니다.
"""

import errno
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codef_connection import validate_credential, validate_public_key, validate_settings


class SettingsWriteError(Exception):
    """파일 경로나 입력값이 들어간 OS 예외 대신 안전한 저장 단계만 알립니다."""


def safe_write_error(stage, error):
    """오류 번호를 안내 문구로 바꿉니다. OS 오류 전문은 출력하지 않습니다."""
    if error.errno == errno.EROFS:
        reason = "/setup-secrets가 읽기 전용입니다. 설정 명령의 쓰기용 볼륨 연결을 확인해 주세요."
    elif error.errno in (errno.EACCES, errno.EPERM):
        reason = "비밀 폴더 권한이 부족합니다. --user 0 옵션과 NAS 폴더 권한을 확인해 주세요."
    elif error.errno == errno.ENOSPC:
        reason = "NAS 저장 공간이 부족합니다. 저장 공간을 확인해 주세요."
    else:
        reason = f"비밀 파일 쓰기를 확인해 주세요. 오류 번호: {error.errno}"
    return SettingsWriteError(f"{stage} 실패: {reason}")


def save_configuration(root, settings):
    """모두 검증한 뒤 파일을 교체합니다. 검증·저장 실패 시 기존 설정은 보존합니다."""
    settings = validate_settings(settings)
    temporary = None
    stage = "비밀 폴더 준비"
    try:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        stage = "비밀 폴더 권한 설정"
        root.chmod(0o700)
        os.chown(root, 1000, 1000)
        stage = "임시 비밀 파일 생성"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=root, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stage = "비밀 파일 권한 설정"
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), 1000, 1000)
            stage = "비밀 파일 저장"
            json.dump(settings, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        stage = "기존 설정 교체"
        temporary.replace(root / "codef-app.json")
    except OSError as error:
        raise safe_write_error(stage, error) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # 정리 실패도 파일명·비밀 값이 포함된 OS 예외로 노출하지 않습니다.
                print("임시 비밀 파일 정리를 완료하지 못했습니다. 비밀 폴더를 확인해 주세요.")


def read_secret(prompt, validator):
    """실패한 항목만 다시 받습니다. 이미 입력한 정상 키를 다시 붙여 넣을 필요가 없습니다."""
    while True:
        value = getpass.getpass(prompt)
        try:
            return validator(value)
        except ValueError as error:
            print(str(error))


def diagnose(root):
    """키를 읽거나 출력하지 않고 실행 사용자·폴더·임시 파일 쓰기만 확인합니다."""
    root = Path(root)
    print("실행 UID:", os.geteuid())
    print("비밀 폴더 존재:", root.is_dir())
    print("기존 설정 파일 존재:", (root / "codef-app.json").is_file())
    if not root.is_dir():
        return
    try:
        with tempfile.TemporaryFile(dir=root) as stream:
            stream.write(b"write-check")
            stream.flush()
        print("임시 파일 쓰기: 정상")
    except OSError as error:
        print(str(safe_write_error("임시 파일 쓰기", error)))


def main():
    """일반 입력은 TTY를 요구하고, --diagnose는 키 없이 볼륨의 쓰기 상태를 확인합니다."""
    root = os.environ.get("MOA_CODEF_SETUP_DIR", "/setup-secrets")
    if sys.argv[1:] == ["--diagnose"]:
        diagnose(root)
        return
    if sys.argv[1:]:
        raise ValueError(
            "추가 인자 없이 실행하거나 --diagnose만 사용해 주세요. 키는 인자로 넣지 않습니다."
        )
    if not sys.stdin.isatty():
        raise ValueError("-it 옵션으로 대화형 터미널에서 실행해 주세요.")
    print("데모버전의 값을 입력하세요. 입력 중에는 글자가 표시되지 않습니다.")
    settings = {"environment": "demo"}
    try:
        settings["client_id"] = read_secret(
            "데모 client_id: ", lambda value: validate_credential("client_id", value)
        )
        print("client_id 형식: 정상")
        settings["client_secret"] = read_secret(
            "데모 client_secret: ", lambda value: validate_credential("client_secret", value)
        )
        print("client_secret 형식: 정상")
        settings["public_key"] = read_secret(
            "public_key (복사한 base64 한 줄): ", validate_public_key
        )
        print("public_key 형식: 정상")
        save_configuration(root, settings)
    finally:
        settings.clear()
    print("데모 키를 저장했습니다. 모아 계정 화면에서 CODEF 개발자 인증 확인을 눌러 주세요.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, SettingsWriteError) as error:
        raise SystemExit("저장하지 못했습니다. " + str(error)) from None
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("입력을 취소했습니다. 기존 설정은 유지됩니다.") from None
