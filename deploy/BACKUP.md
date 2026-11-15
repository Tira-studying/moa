# DB 자동 백업과 복원

Mac에서 실행 중인 모아의 DB(`moa_card-data` 볼륨의 `cards.sqlite3`)를 매일 Mac의 개인 폴더로 백업합니다. 백업 파일에는 실제 거래·계정 정보가 들어 있으므로 GitHub나 채팅에 올리지 않습니다.

## 1. 동작 방식

- 매일 **04:30**에 macOS launchd가 [`scripts/backup-db.sh`](../scripts/backup-db.sh)를 실행합니다. Mac이 잠자기 중이었다면 깨어난 뒤 실행합니다.
- 서버를 멈추지 않습니다. 앱 컨테이너 안에서 SQLite 온라인 백업을 만들고 무결성 검사(`integrity_check`)를 통과한 사본만 가져옵니다.
- 사본은 컨테이너의 메모리 임시 폴더에서만 잠시 만들어지므로 DB 볼륨 안에 백업이 쌓이지 않습니다.
- 저장 위치: `~/moa-backups/cards-auto-날짜-시각.sqlite3` (폴더 700, 파일 600 권한)
- 두 번째 저장 위치: NAS의 백업 폴더(예: `/volume1/moa-backup`) (아래 "NAS 복사" 참고)
- 자동 백업은 최근 **30개**만 남깁니다. 업데이트 전에 직접 만든 `cards-before-*` 같은 백업은 자동으로 지우지 않습니다.
- 결과는 `~/moa-backups/backup.log`에 한 줄씩 남습니다. 실패하면 Mac 알림이 뜹니다.

```text
2026-11-03 04:30:02 완료: cards-auto-20261103-043001.sqlite3 (구조 버전 2, 거래 131건)
```

Docker Desktop이 꺼져 있으면 백업이 실패하고 알림이 뜹니다. 서버 자체도 멈춘 상태이므로 Docker Desktop부터 확인합니다.

### NAS 복사

Mac 디스크가 고장 나도 백업이 남도록 매번 NAS에도 SSH로 복사합니다.

- 주소: NAS의 **Tailscale 주소**(예: `100.x.y.z`)와 NAS의 SSH 포트. 집 내부 주소(192.168.x.x)는 macOS의 로컬 네트워크 권한 때문에 앱·launchd에서 막힐 수 있어 사용하지 않습니다.
- 키: 백업 전용 `~/.ssh/moa_nas_backup`(암호 없음). NAS의 `~/.ssh/authorized_keys`에 `restrict` 옵션과 함께 등록되어 있습니다. 다른 용도로 쓰지 않습니다.
- NAS 공유 폴더는 상속 권한 때문에 새 파일이 모두 열람 가능하게 만들어질 수 있어, 복사할 때마다 폴더 700·파일 600으로 다시 잠급니다.
- NAS에서도 자동 백업은 최근 30개만 남깁니다.
- NAS 복사가 실패해도 Mac 백업은 끝난 상태입니다. 로그에 `경고: 원격 복사 실패`가 남고 경고 알림이 뜹니다. NAS 전원·Tailscale 연결을 확인합니다.

NAS에서 복원 파일을 가져올 때:

```sh
scp -P 22 -i ~/.ssh/moa_nas_backup YOUR-NAS-USER@NAS-TAILSCALE-IP:/volume1/moa-backup/파일이름.sqlite3 ~/moa-backups/
```

## 2. 설치·확인·해제

설치(처음 한 번, 이미 설치됨):

```sh
cp deploy/com.moa.db-backup.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.moa.db-backup.plist
```

지금 바로 한 번 실행:

```sh
launchctl kickstart -k gui/$(id -u)/com.moa.db-backup
tail -3 ~/moa-backups/backup.log
```

해제:

```sh
launchctl bootout gui/$(id -u)/com.moa.db-backup
rm ~/Library/LaunchAgents/com.moa.db-backup.plist
```

## 3. 설정 바꾸기

`scripts/backup-db.sh`는 환경 변수로 설정합니다. launchd에서 바꾸려면 plist의 `EnvironmentVariables`에 추가하고 해제 → 설치를 다시 합니다.

| 변수                     | 기본값                  | 뜻                                                                    |
| ------------------------ | ----------------------- | --------------------------------------------------------------------- |
| `MOA_BACKUP_DIR`         | `~/moa-backups`         | 백업 폴더                                                             |
| `MOA_BACKUP_KEEP`        | `30`                    | 자동 백업 보관 개수                                                   |
| `MOA_BACKUP_MIRROR`      | 없음                    | 두 번째 보관 폴더(외장 디스크·NAS 연결 폴더). 연결되어 있을 때만 복사 |
| `MOA_BACKUP_REMOTE`      | 없음                    | SSH 복사 대상 `사용자@주소:/폴더` (launchd에 NAS로 설정됨)            |
| `MOA_BACKUP_REMOTE_PORT` | `22`                    | 원격 SSH 포트 (NAS는 `222`)                                           |
| `MOA_BACKUP_SSH_KEY`     | `~/.ssh/moa_nas_backup` | 원격 복사 전용 키                                                     |
| `MOA_APP_CONTAINER`      | `moa-app-1`             | 앱 컨테이너 이름                                                      |

## 4. 복원

복원하면 백업 시점 이후의 거래·분류·납부 기록이 사라집니다. 복원 전에 현재 DB도 한 번 더 백업합니다.

```sh
cd "$HOME/moa-projects/moa"
scripts/backup-db.sh                       # 현재 상태도 남겨 둡니다
cd deploy
docker compose -f compose.nas.yml -f compose.mac.yml --profile codef --profile mail stop app codef-worker mail-poller
docker run --rm --user 0 --entrypoint sh \
  -v moa_card-data:/data -v "$HOME/moa-backups":/backup:ro \
  moa-app:latest -c 'cp /backup/복원할파일.sqlite3 /data/cards.sqlite3 && chown 1000:1000 /data/cards.sqlite3 && chmod 600 /data/cards.sqlite3'
docker compose -f compose.nas.yml -f compose.mac.yml --profile codef --profile mail up -d app codef-worker mail-poller
```

`복원할파일.sqlite3`를 실제 파일 이름으로 바꿉니다. 예전 구조 버전의 백업을 복원해도 앱이 시작할 때 `migrations.py`가 최신 구조로 올립니다.
