# Mac(Apple Silicon)으로 모아 이전하기

Apple Silicon Mac은 ARM64 CPU입니다. 모아는 Docker Desktop의 Linux 컨테이너에서 실행하도록 구성합니다. NAS에서 만든 x86 이미지를 복사하는 대신, **같은 코드를 맥에서 ARM64 이미지로 다시 빌드**하고 데이터를 복원합니다. 실제 Mac 기기에서의 이미지 빌드·Chromium 샌드박스 실행은 이전할 맥에서 확인해야 합니다. 판독기에 있는 `Linux i686` 표시는 구형 카드사 스크립트에 보여주는 호환용 문자열이며, 컨테이너를 x86으로 실행한다는 뜻은 아닙니다.

기본 [compose.nas.yml](compose.nas.yml)에 [compose.mac.yml](compose.mac.yml)을 함께 적용합니다. 추가 파일은 ARM64를 지정하고 비밀 파일 저장소를 Mac용 Docker 볼륨으로 대체합니다. 브라우저 샌드박스·공식 연결 제한·가계부 DB 구조와 볼륨 이름은 유지합니다.

기존 공유 원장 버전에서 동시에 업데이트한다면 [개인 관리·통합 공유 안내](../docs/PERSONAL_SHARING.md)도 확인하세요. 기존 자료는 소유자 계정에 보존하고 로그인 세션은 종료하며, 통합 화면에는 합계만 공유합니다.

## 1. 옮길 것과 유지할 것

| 대상 | 내용 |
| --- | --- |
| GitHub 저장소 | 앱 코드·배포 설정 |
| NAS `moa_card-data` 볼륨 | 거래·분류·할부·납부·계정·변경 이력·메일 수집 위치 |
| NAS `moa_tailscale-state` 볼륨 | 앱용 Tailscale 장치 등록·주소·Serve 설정 |
| NAS `deploy/secrets` 폴더 전체 | Gmail 앱 암호·공통 명세서 암호·개인별 `users/` 설정·CODEF `codef-app.json` |
| NAS `deploy/.env` | 공개 접속 주소·Gmail 계정 등 설정 |

NAS 본체의 SSH용 Tailscale은 옮기거나 종료하지 않습니다. 아래 절차는 **모아 Docker 프로젝트의 tailscale 서비스**만 대상으로 합니다.

문자 수신 키·개인 계정·이메일 처리 상태는 DB와 함께 옮깁니다. 비밀 폴더는 일부 파일만 고르지 말고 하위 폴더까지 통째로 백업합니다. CODEF 카드사 로그인·이름·본인 확인용 입력은 시험 중 메모리에서만 사용했으므로 백업 대상에 없으며, 이전 후 시험할 때 다시 입력합니다. CODEF 키를 옮겨도 미구현인 정기 수집이 새로 시작되는 것은 아닙니다.

Tailscale 상태를 복원하면 기존 장치 식별과 주소를 이어 사용할 수 있지만, 장치 키 만료나 관리자 정책에 따라 재인증이 필요할 수 있습니다. 기존 이름·HTTPS 주소·공유 상태를 전환 후 확인합니다. 새 장치로 등록되었다면 실제 주소를 `.env`에 반영하고 공동관리자 공유도 다시 확인해야 합니다.

## 2. Mac 준비 — NAS를 멈추기 전에

1. Apple Silicon용 Docker Desktop을 설치하고 실행합니다. Linux ARM64 컨테이너를 사용합니다.
2. Mac의 터미널에서 아래 명령을 확인합니다.

```sh
uname -m
docker version
docker compose version
```

첫 명령의 예상 결과는 `arm64`입니다. Docker Desktop이 실행된 상태여야 뒤 명령들이 정상 작동합니다.

3. 저장소를 내려받습니다. 비공개 저장소이므로 GitHub SSH 키 또는 기존 인증을 사용합니다. 토큰을 URL에 넣지 않습니다.

```sh
mkdir -p "$HOME/moa-projects"
cd "$HOME/moa-projects"
git clone --branch main git@github.com:Tira-studying/moa.git moa
cd moa
```

Mac에서 Git 사용이 처음이면 macOS가 명령줄 도구 설치를 안내할 수 있습니다. NAS의 배포 키나 개인키를 그대로 복사할 필요는 없습니다. Mac용 키를 별도로 등록하거나 인증된 GitHub 도구로 코드를 받으면 됩니다.

현재 NAS와 **같은 코드 버전**으로 먼저 이전하려면 NAS SSH 터미널에서 작업 폴더의 커밋을 확인합니다. 이 명령은 파일을 수정하거나 NAS 앱을 중지하지 않습니다.

```sh
sudo docker run --rm \
  -v "$HOME/moa-projects:/git:ro" -w /git/moa \
  alpine/git -c safe.directory=/git/moa rev-parse HEAD
```

Mac의 `git rev-parse HEAD` 결과와 비교합니다. 다르면 NAS에서 나온 커밋 해시를 기록하고 Mac에서 `git switch --detach 해당커밋해시`로 그 버전을 선택한 뒤 빌드할 수 있습니다. NAS 작업 폴더에 미커밋 수정이 있거나 업데이트 후 이미지를 재빌드하지 않았다면 작업 폴더 커밋만으로 실행 중 이미지 버전을 보증할 수 없으므로 전환 전에 확인합니다. 이전 중 기능 업데이트를 함께 적용할 경우에도 원래 NAS 버전과 백업은 남겨 둡니다.

4. NAS 데이터 없이 먼저 이미지를 빌드하고 샌드박스를 확인합니다.

```sh
docker build --platform linux/arm64 -t moa-mac-preflight .
docker run --rm \
  --security-opt "seccomp=$PWD/deploy/playwright-seccomp.json" \
  --read-only \
  --tmpfs /tmp:rw,size=512m,mode=1777 \
  --shm-size 256m \
  --pids-limit 256 \
  --memory 2g \
  moa-mac-preflight node scripts/check-browser.cjs
```

`Chromium sandbox: OK`가 나오는지 확인합니다. 실패하면 **NAS는 계속 사용하고**, 오류에 맞춰 Docker Desktop의 샌드박스 지원부터 확인합니다. `--no-sandbox`나 `privileged`로 우회하지 않습니다.

전체 가상 명세서·화면 검증도 실행할 수 있습니다. NAS DB나 비밀 파일은 연결하지 않습니다.

```sh
docker run --rm \
  --security-opt "seccomp=$PWD/deploy/playwright-seccomp.json" \
  --read-only \
  --tmpfs /tmp:rw,size=512m,mode=1777 \
  --shm-size 256m \
  --pids-limit 256 \
  --memory 2g \
  -e MOA_BROWSER_TESTS=1 \
  moa-mac-preflight python -m unittest discover -s tests -q
```

## 3. NAS에서 최종 백업

이 단계부터 전환이 끝날 때까지 모아 접속이 중단됩니다. 공동관리자에게도 편집을 멈추도록 안내합니다. 아래는 **NAS SSH 터미널**에서 실행합니다. Mac 터미널과 구분하세요.

같은 Tailscale 장치 상태를 NAS와 맥에서 동시에 켜면 안 됩니다. NAS의 앱·메일 수집을 먼저 멈추고 앱용 Tailscale도 멈춘 상태에서 백업합니다. 컨테이너와 볼륨을 삭제하지 않습니다.

백업용 Alpine 이미지는 중지 전에 미리 준비해 다운로드 때문에 접속 중단 시간이 길어지지 않도록 합니다.

```sh
sudo docker pull alpine:3.21
```

```sh
cd "$HOME/moa-projects/moa/deploy"
sudo docker compose -f compose.nas.yml --profile mail stop app mail-poller tailscale
```

파일을 덮어쓰지 않도록 시간 이름을 가진 백업 폴더를 만듭니다.

```sh
MOA_BACKUP_DIR="$HOME/moa-migration-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$MOA_BACKUP_DIR"
sudo docker run --rm \
  -v moa_card-data:/source:ro \
  -v "$MOA_BACKUP_DIR":/backup \
  alpine:3.21 sh -eu -c 'umask 077; tar -czf /backup/card-data.tar.gz -C /source .'
sudo docker run --rm \
  -v moa_tailscale-state:/source:ro \
  -v "$MOA_BACKUP_DIR":/backup \
  alpine:3.21 sh -eu -c 'umask 077; tar -czf /backup/tailscale-state.tar.gz -C /source .'
sudo tar -czf "$MOA_BACKUP_DIR/secrets.tar.gz" -C secrets .
cp .env "$MOA_BACKUP_DIR/moa.env"
sudo chown "$(id -u):$(id -g)" \
  "$MOA_BACKUP_DIR/card-data.tar.gz" \
  "$MOA_BACKUP_DIR/tailscale-state.tar.gz" \
  "$MOA_BACKUP_DIR/secrets.tar.gz" \
  "$MOA_BACKUP_DIR/moa.env"
chmod 600 "$MOA_BACKUP_DIR"/*
```

`secrets` 폴더가 없는 수동 업로드 전용 설치는 빈 폴더를 만들어 빈 `secrets.tar.gz`를 준비할 수 있습니다. 기존에 사용 중인 비밀 파일을 빈 것으로 바꾸라는 뜻은 아닙니다.

백업 파일 네 개를 Mac의 개인 폴더로 복사합니다. Finder의 NAS 연결이나 SCP를 사용할 수 있습니다. SSH 연결은 NAS의 기존 LAN 주소 또는 **NAS 본체 Tailscale 주소**를 사용합니다. 중지한 모아 장치 주소로 SSH 접속을 시도하지 않습니다. 파일 이름 네 개와 크기가 0이 아닌지 확인하되 비밀 파일·백업의 내용을 화면에 출력하지 않습니다.

백업에는 거래·계정·실제 암호·Tailscale 장치 키가 포함됩니다. GitHub나 채팅에 올리지 않습니다. Mac에서 복사본 폴더 이름을 `~/moa-migration-backup`으로 맞추면 다음 명령을 그대로 사용할 수 있습니다.

## 4. Mac에 설정과 데이터를 복원

이제 **Mac 터미널**에서 실행합니다. NAS의 모아 컨테이너는 계속 중지한 상태로 둡니다.

```sh
cd "$HOME/moa-projects/moa/deploy"
cp "$HOME/moa-migration-backup/moa.env" .env
chmod 600 .env
MOA_BACKUP_DIR="$HOME/moa-migration-backup"
chmod 700 "$MOA_BACKUP_DIR"
docker volume create moa_card-data
docker volume create moa_tailscale-state
docker volume create moa_mac-secrets
```

아래 복원 명령은 비어 있는 볼륨에만 적용합니다. 기존 Mac 설치가 있어 볼륨에 파일이 있으면 멈추도록 구성했습니다. 그 경우 기존 볼륨을 바로 삭제하지 말고 어느 설치의 자료인지 확인하세요.

```sh
docker run --rm \
  -v moa_card-data:/restore \
  -v "$MOA_BACKUP_DIR":/backup:ro \
  alpine:3.21 sh -eu -c '
    test -z "$(ls -A /restore)"
    tar -xzf /backup/card-data.tar.gz -C /restore
    chown -R 1000:1000 /restore
  '
docker run --rm \
  -v moa_tailscale-state:/restore \
  -v "$MOA_BACKUP_DIR":/backup:ro \
  alpine:3.21 sh -eu -c '
    test -z "$(ls -A /restore)"
    tar -xzf /backup/tailscale-state.tar.gz -C /restore
  '
docker run --rm \
  -v moa_mac-secrets:/restore \
  -v "$MOA_BACKUP_DIR":/backup:ro \
  alpine:3.21 sh -eu -c '
    test -z "$(ls -A /restore)"
    tar -xzf /backup/secrets.tar.gz -C /restore
    chown -R 1000:1000 /restore
    find /restore -type d -exec chmod 700 {} +
    find /restore -type f -exec chmod 600 {} +
  '
```

Mac 설정에서는 호스트의 `deploy/secrets` 대신 `moa_mac-secrets` 볼륨을 읽습니다. Mac 사용자와 컨테이너의 UID가 달라도 앱 사용자 UID 1000이 읽을 수 있도록 복원한 것입니다. 비밀 파일을 갱신할 때도 이 볼륨을 대상으로 해야 합니다.

Mac 추가 설정은 `moa_card-data`, `moa_tailscale-state`, `moa_mac-secrets`를 명시된 이름의 **external 볼륨**으로 사용합니다. 위에서 생성·복원한 볼륨을 그대로 사용하며 Compose가 빈 저장소를 자동으로 만들지 않습니다. 구버전의 ‘already exists but was not created by Docker Compose’ 경고를 없애기 위해 볼륨을 삭제할 필요는 없습니다. `external volume not found`가 나오면 복원 단계와 실제 볼륨 이름부터 확인합니다.

## 5. Mac에서 실행하고 확인

Mac에서는 **항상 두 Compose 파일을 함께 지정**합니다. 기본 파일만 사용하면 NAS용 비밀 파일 경로를 읽게 됩니다.

```sh
cd "$HOME/moa-projects/moa/deploy"
docker compose -f compose.nas.yml -f compose.mac.yml --profile mail build app mail-poller
docker compose -f compose.nas.yml -f compose.mac.yml up -d tailscale app
docker compose -f compose.nas.yml -f compose.mac.yml ps
docker compose -f compose.nas.yml -f compose.mac.yml exec tailscale tailscale status
docker compose -f compose.nas.yml -f compose.mac.yml exec tailscale tailscale serve status
```

기존 전체 HTTPS 주소가 표시되고 앱이 `healthy`가 되는지 확인합니다. 필요할 때만 Tailscale에 재인증합니다. Serve 설정이 없으면 기존 설치와 같은 프록시를 설정합니다.

```sh
docker compose -f compose.nas.yml -f compose.mac.yml exec tailscale tailscale serve --bg --https=443 http://127.0.0.1:8000
```

확인할 항목:

- 아이폰·안드로이드에서 Tailscale을 켜고 기존 모아 HTTPS 주소로 접속
- 기존 소유자·공동관리자 아이디와 비밀번호로 로그인
- 저장 기록·거래 건수·분류·할부·납부 기록이 이전과 같은지 확인
- 기존 공동관리자의 Tailscale 장치 공유가 유지되는지 확인
- 실제 명세서는 우선 미리보기까지만 확인하고 거래는 중복 저장하지 않기

검증이 끝나면 메일 수집을 시작합니다.

```sh
docker compose -f compose.nas.yml -f compose.mac.yml --profile mail up -d mail-poller
```

이메일 화면에서 ‘목록 새로고침’으로 연결을 확인합니다. NAS 메일 수집은 계속 중지 상태여야 합니다. 이제 Mac을 운영 대상으로 사용합니다.

CODEF는 계정 화면의 설정 상태 새로고침으로 기존 키 파일을 읽는지 먼저 확인합니다. 카드사 실제 조회는 별도 요청이므로 이전 확인만을 위해 반복 실행하지 않습니다. 필요한 수동 시험은 Mac에서도 두 Compose 파일을 함께 사용합니다.

```sh
docker compose -f compose.nas.yml -f compose.mac.yml run --rm --no-deps \
  app python scripts/verify-codef-card.py --card samsung
```

기존 모아 HTTPS 주소가 유지됐다면 휴대폰 문자 자동화의 URL·DB에 저장된 수신 키도 이어 사용할 수 있습니다. 주소가 바뀌었다면 자동화 URL과 `.env`의 `MOA_PUBLIC_ORIGIN`을 실제 주소에 맞춰 갱신합니다. 단축어의 수신 키를 새로 발급하거나 이름·인증 정보를 채팅에 보내는 것은 이전 절차가 아닙니다.

## 6. 중단 없는 운영과 되돌리기

Docker Desktop은 Mac 사용자 로그인 후 실행되는 프로그램입니다. 로그인 시 시작 설정과 재부팅 후 Docker 실행을 확인하세요. 맥이 잠자기에 들어가면 모아 접속·Gmail 수집이 중단되므로 macOS 전원 설정에서 필요한 상시 실행 상태를 유지합니다. 화면만 꺼지는 것과 컴퓨터가 잠자는 것은 다릅니다.

아직 Mac에서 실제 기록을 수정하지 않았다면, 문제가 있을 때 **Mac의 세 서비스를 먼저 중지한 뒤** NAS를 다시 시작할 수 있습니다.

```sh
# Mac 터미널
docker compose -f compose.nas.yml -f compose.mac.yml --profile mail stop app mail-poller tailscale
```

```sh
# NAS SSH 터미널
cd "$HOME/moa-projects/moa/deploy"
sudo docker compose -f compose.nas.yml --profile mail up -d tailscale app mail-poller
```

Mac에서 새 거래·분류·계정·납부 기록을 변경한 뒤라면 NAS의 DB는 이전 시점 자료입니다. 그때는 Mac의 최신 DB를 중지·백업한 뒤 되돌려야 하며, NAS를 바로 켜서 두 자료를 자동으로 합칠 수는 없습니다.

이 안내는 이전 구성을 준비한 것이며 사용자의 NAS나 Mac을 원격으로 중지·이전하지 않았습니다. 실제 Mac 검증과 전환 완료 전에는 NAS 데이터와 백업을 유지하세요.
