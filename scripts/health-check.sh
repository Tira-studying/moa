#!/bin/bash
# 모아 서버 상태 확인: 5분마다 launchd가 실행합니다. 멈춘 부분은 안전한 범위에서 다시 켜고, 계속 안 되면 휴대폰에 알립니다.
#
# 확인 순서
#   1. Docker Desktop이 응답하는지 → 꺼져 있으면 실행합니다(재부팅 뒤 자동 시작 역할도 합니다).
#   2. 컨테이너 4개(app·codef-worker·mail-poller·tailscale)가 실행 중인지 → 멈춰 있으면 docker start로 다시 켭니다.
#      컨테이너를 지우거나 새로 만들지는 않습니다. 이미지·볼륨·설정은 건드리지 않습니다.
#   3. 앱 상태 검사(healthcheck)가 healthy인지
#   4. 실제 주소(https://moa-card...)가 Tailscale을 거쳐 응답하는지
#
# 알림 규칙: 2회 연속(약 10분) 실패하면 한 번 알리고, 회복되면 한 번 더 알립니다. 같은 장애로 반복해서 알리지 않습니다.
# 알림 내용에는 거래·계정 같은 개인정보를 넣지 않습니다.
#
# 설정 파일 ~/.config/moa/alert.env (저장소에 넣지 않습니다)
#   MOA_NTFY_TOPIC=추측하기 어려운 주제 이름   # ntfy 앱에서 같은 주제를 구독하면 휴대폰 알림을 받습니다
#   MOA_NTFY_SERVER=https://ntfy.sh             # 선택
#   MOA_HEALTH_URL=https://moa-card.YOUR-TAILNET.ts.net/login   # 선택
#
# 기록: ~/.local/state/moa-health/health.log (상태가 바뀔 때만 남깁니다)

set -uo pipefail

CONFIG="$HOME/.config/moa/alert.env"
[ -f "$CONFIG" ] && . "$CONFIG"
NTFY_SERVER="${MOA_NTFY_SERVER:-https://ntfy.sh}"
NTFY_TOPIC="${MOA_NTFY_TOPIC:-}"
URL="${MOA_HEALTH_URL:-https://moa-card.YOUR-TAILNET.ts.net/login}"
CONTAINERS=(moa-app-1 moa-codef-worker-1 moa-mail-poller-1 moa-tailscale-1)
ALERT_AFTER=2
DOCKER="${DOCKER:-$(command -v docker || echo /usr/local/bin/docker)}"

STATE_DIR="$HOME/.local/state/moa-health"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
LOG="$STATE_DIR/health.log"
FAILS_FILE="$STATE_DIR/failures"
ALERTED_FILE="$STATE_DIR/alerted"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"
}

# 휴대폰(ntfy)과 Mac 알림을 함께 보냅니다. 주제가 설정되지 않았으면 Mac 알림만 띄웁니다.
notify() {
  local title="$1" message="$2" priority="$3"
  /usr/bin/osascript -e "display notification \"$message\" with title \"$title\"" 2>/dev/null || true
  if [ -n "$NTFY_TOPIC" ]; then
    /usr/bin/curl -sS --max-time 15 -o /dev/null \
      -H "Title: $title" -H "Priority: $priority" -H "Tags: moa" \
      -d "$message" "$NTFY_SERVER/$NTFY_TOPIC" || log "경고: 휴대폰 알림 전송 실패"
  fi
}

problems=()

# 1. Docker Desktop
if ! "$DOCKER" info >/dev/null 2>&1; then
  log "Docker 응답 없음 → Docker Desktop 실행"
  /usr/bin/open -ga Docker
  for _ in $(seq 1 18); do
    sleep 5
    "$DOCKER" info >/dev/null 2>&1 && break
  done
  "$DOCKER" info >/dev/null 2>&1 || problems+=("Docker가 켜지지 않음")
fi

# 2·3. 컨테이너 실행 상태와 앱 상태 검사
if [ ${#problems[@]} -eq 0 ]; then
  for name in "${CONTAINERS[@]}"; do
    running="$("$DOCKER" inspect -f '{{.State.Running}}' "$name" 2>/dev/null)"
    if [ "$running" != "true" ]; then
      log "$name 멈춤 → docker start"
      "$DOCKER" start "$name" >/dev/null 2>&1 || problems+=("$name 시작 실패")
    fi
  done
  health="$("$DOCKER" inspect -f '{{.State.Health.Status}}' moa-app-1 2>/dev/null)"
  # 방금 다시 켠 경우 starting일 수 있어 그때는 다음 확인에서 판단합니다.
  if [ "$health" != "healthy" ] && [ "$health" != "starting" ]; then
    problems+=("앱 상태 검사: ${health:-알 수 없음}")
  fi
fi

# 4. 실제 접속 주소
code="$(/usr/bin/curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$URL" 2>/dev/null)"
if [ "$code" != "200" ]; then
  problems+=("접속 주소 응답: ${code:-없음}")
fi

# 알림 판단
fails="$(cat "$FAILS_FILE" 2>/dev/null || echo 0)"
if [ ${#problems[@]} -eq 0 ]; then
  if [ -f "$ALERTED_FILE" ]; then
    notify "모아 서버 회복" "모아 서버가 다시 정상입니다." default
    log "회복"
    rm -f "$ALERTED_FILE"
  elif [ "$fails" -gt 0 ]; then
    log "일시 오류 후 정상"
  fi
  echo 0 >"$FAILS_FILE"
  exit 0
fi

fails=$((fails + 1))
echo "$fails" >"$FAILS_FILE"
summary="$(IFS=', '; echo "${problems[*]}")"
log "실패 $fails회: $summary"
if [ "$fails" -ge "$ALERT_AFTER" ] && [ ! -f "$ALERTED_FILE" ]; then
  notify "모아 서버 이상" "$summary" high
  touch "$ALERTED_FILE"
fi
exit 1
