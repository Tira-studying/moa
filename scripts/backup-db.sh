#!/bin/bash
# 모아 DB 자동 백업: 실행 중인 앱 컨테이너에서 SQLite 온라인 백업을 만들어 Mac의 개인 폴더로 옮깁니다.
#
# 서버를 멈추지 않습니다. SQLite 백업 API는 쓰기 중에도 일관된 시점의 사본을 만듭니다.
# 사본은 컨테이너의 메모리 임시 폴더(/tmp, tmpfs)에서만 잠시 만들고, 옮긴 뒤 바로 지웁니다.
# 그래서 DB 볼륨(/data) 안에 백업 사본이 쌓이지 않습니다.
#
# 보관 규칙: 자동 백업(cards-auto-*.sqlite3)만 최근 MOA_BACKUP_KEEP개(기본 30개)를 남깁니다.
# 직접 만든 백업(cards-before-* 등)은 이름이 달라 자동으로 지우지 않습니다.
#
# 설정(환경 변수, 모두 선택):
#   MOA_BACKUP_DIR        백업 폴더 (기본 ~/moa-backups)
#   MOA_BACKUP_KEEP       자동 백업 보관 개수 (기본 30)
#   MOA_BACKUP_MIRROR     두 번째 보관 폴더 (예: 외장 디스크·NAS 연결 폴더). 있을 때만 복사합니다.
#   MOA_APP_CONTAINER     앱 컨테이너 이름 (기본 moa-app-1)
#   MOA_BACKUP_REMOTE     SSH로 복사할 원격 폴더 (예: 사용자@NAS-TAILSCALE-IP:/volume1/moa-backup)
#   MOA_BACKUP_REMOTE_PORT  원격 SSH 포트 (기본 22)
#   MOA_BACKUP_SSH_KEY    원격 복사 전용 키 (기본 ~/.ssh/moa_nas_backup)
#
# 실행: scripts/backup-db.sh   (매일 자동 실행은 deploy/BACKUP.md의 launchd 설정 참고)

set -euo pipefail
umask 077

BACKUP_DIR="${MOA_BACKUP_DIR:-$HOME/moa-backups}"
KEEP="${MOA_BACKUP_KEEP:-30}"
MIRROR="${MOA_BACKUP_MIRROR:-}"
REMOTE="${MOA_BACKUP_REMOTE:-}"
REMOTE_PORT="${MOA_BACKUP_REMOTE_PORT:-22}"
SSH_KEY="${MOA_BACKUP_SSH_KEY:-$HOME/.ssh/moa_nas_backup}"
CONTAINER="${MOA_APP_CONTAINER:-moa-app-1}"
DOCKER="${DOCKER:-$(command -v docker || echo /usr/local/bin/docker)}"
STAMP="$(date +%Y%m%d-%H%M%S)"
NAME="cards-auto-$STAMP.sqlite3"
LOG="$BACKUP_DIR/backup.log"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"
}

# 실패하면 기록을 남기고 Mac 알림을 띄웁니다. 백업이 조용히 멈추는 일을 막기 위해서입니다.
fail() {
  log "실패: $1"
  /usr/bin/osascript -e "display notification \"$1\" with title \"모아 DB 백업 실패\"" 2>/dev/null || true
  exit 1
}
trap 'fail "예상하지 못한 오류 (줄 $LINENO)"' ERR

"$DOCKER" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true ||
  fail "앱 컨테이너($CONTAINER)가 실행 중이 아닙니다. Docker Desktop이 켜져 있는지 확인하세요."

# 1. 컨테이너 안에서 백업과 무결성 검사를 합니다. 거래 수를 함께 기록해 이상 여부를 알아보기 쉽게 합니다.
summary="$("$DOCKER" exec -i "$CONTAINER" python - <<'PY'
import os
import sqlite3

os.umask(0o077)
target = "/tmp/moa-backup.sqlite3"
if os.path.exists(target):
    os.remove(target)
source = sqlite3.connect(os.environ.get("CARD_DB", "/data/cards.sqlite3"))
copy = sqlite3.connect(target)
source.backup(copy)
source.close()
check = copy.execute("PRAGMA integrity_check").fetchone()[0]
version = copy.execute("PRAGMA user_version").fetchone()[0]
count = copy.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
copy.close()
if check != "ok":
    raise SystemExit(f"integrity_check={check}")
print(f"구조 버전 {version}, 거래 {count}건")
PY
)" || fail "컨테이너 안에서 백업을 만들지 못했습니다."

# 2. Mac으로 옮기고 컨테이너의 임시 사본을 지웁니다.
# docker cp는 tmpfs(/tmp)를 읽지 못하므로 cat 출력으로 받습니다.
"$DOCKER" exec "$CONTAINER" cat /tmp/moa-backup.sqlite3 >"$BACKUP_DIR/$NAME" ||
  fail "백업 파일을 Mac으로 옮기지 못했습니다."
"$DOCKER" exec "$CONTAINER" rm -f /tmp/moa-backup.sqlite3 || true
chmod 600 "$BACKUP_DIR/$NAME"
[ -s "$BACKUP_DIR/$NAME" ] || fail "백업 파일이 비어 있습니다."

# 3. 두 번째 보관 폴더가 연결되어 있으면 복사합니다. 연결이 없으면 건너뜁니다.
if [ -n "$MIRROR" ]; then
  if [ -d "$MIRROR" ]; then
    cp -p "$BACKUP_DIR/$NAME" "$MIRROR/$NAME" || log "경고: 두 번째 폴더 복사 실패 ($MIRROR)"
  else
    log "경고: 두 번째 폴더가 연결되어 있지 않아 건너뜀 ($MIRROR)"
  fi
fi

# 4. 원격(NAS)에 SSH로 복사합니다. Mac 디스크가 고장 나도 백업이 남게 하기 위해서입니다.
# scp 대신 cat 전송을 쓰는 이유: NAS에 SFTP가 없어도 동작하고, .part로 받은 뒤 이름을 바꿔
# 전송이 끊겨도 반쯤 받은 파일이 정상 백업처럼 보이지 않게 합니다.
# 원격 복사가 실패해도 Mac의 백업은 이미 끝났으므로 경고 알림만 띄웁니다.
if [ -n "$REMOTE" ]; then
  host="${REMOTE%%:*}"
  dir="${REMOTE#*:}"
  ssh_cmd=(/usr/bin/ssh -p "$REMOTE_PORT" -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes
    -o ConnectTimeout=15 -o StrictHostKeyChecking=yes "$host")
  # NAS 공유 폴더는 상속 권한 때문에 umask와 상관없이 777로 만들어질 수 있어 chmod로 다시 잠급니다.
  if "${ssh_cmd[@]}" "umask 077; mkdir -p '$dir' && chmod 700 '$dir' && cat > '$dir/$NAME.part' && mv '$dir/$NAME.part' '$dir/$NAME' && chmod 600 '$dir/$NAME'" \
    <"$BACKUP_DIR/$NAME"; then
    # 원격에서도 자동 백업만 최근 KEEP개를 남깁니다.
    "${ssh_cmd[@]}" "cd '$dir' && ls -1t cards-auto-*.sqlite3 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do rm -f \"\$f\"; done" ||
      log "경고: 원격 오래된 백업 정리 실패"
    log "원격 복사: $host:$dir/$NAME"
  else
    log "경고: 원격 복사 실패 ($host, 포트 $REMOTE_PORT)"
    /usr/bin/osascript -e 'display notification "NAS 복사에 실패했습니다. Mac 백업은 완료됐습니다." with title "모아 DB 백업 경고"' 2>/dev/null || true
  fi
fi

# 5. 오래된 자동 백업을 정리합니다. 자동 백업 이름(cards-auto-*)만 대상으로 합니다.
ls -1t "$BACKUP_DIR"/cards-auto-*.sqlite3 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
done

log "완료: $NAME ($summary)"
echo "$NAME ($summary)"
