# 자동 테스트·서버 상태 알림·재부팅 복구

Mac에서 운영하는 모아를 지키는 장치 세 가지입니다. DB 백업은 [DB 자동 백업과 복원](BACKUP.md)에 따로 있습니다.

## 1. 자동 테스트 (GitHub Actions)

[`.github/workflows/test.yml`](../.github/workflows/test.yml)이 main에 push하거나 PR을 열 때 실행됩니다.

- 운영과 같은 Docker 이미지를 만든 뒤, 그 안에서 전체 테스트를 화면 테스트까지 포함해 실행합니다.
- Dockerfile에 새 파일을 빠뜨리면 이미지 안에서 import가 실패하므로 여기서 잡힙니다.
- 결과는 GitHub 저장소의 Actions 탭에서 봅니다. 실패하면 GitHub가 메일로 알려 줍니다.
- 터미널에서 확인: `gh run list --limit 5`

배포 전에 최신 실행이 성공(✓)인지 확인합니다.

## 2. 서버 상태 알림

[`scripts/health-check.sh`](../scripts/health-check.sh)를 launchd 작업 `com.moa.health-check`가 **5분마다**, 그리고 로그인 직후에 실행합니다.

| 확인                                            | 문제가 있으면                                        |
| ----------------------------------------------- | ---------------------------------------------------- |
| Docker Desktop 응답                             | Docker Desktop을 실행                                |
| 컨테이너 app·codef-worker·tailscale 실행        | `docker start`로 다시 켬 (지우거나 새로 만들지 않음) |
| 앱 상태 검사(healthcheck)                       | 알림 대상                                            |
| `https://moa-card.YOUR-TAILNET.ts.net/login` 응답 | 알림 대상                                            |

2회 연속(약 10분) 실패하면 **"모아 서버 이상"**을 한 번 보내고, 회복되면 **"모아 서버 회복"**을 보냅니다. 알림에는 개인정보를 넣지 않습니다.

### 휴대폰에서 알림 받기 (처음 한 번)

알림은 계정이 필요 없는 [ntfy](https://ntfy.sh)로 보냅니다.

1. iPhone App Store에서 **ntfy** 앱을 설치합니다.
2. 앱에서 **+ (Subscribe to topic)** → 주제 이름에 Mac의 `~/.config/moa/alert.env`에 있는 `MOA_NTFY_TOPIC` 값을 입력합니다.
   ```sh
   cat ~/.config/moa/alert.env
   ```
3. 주제 이름은 비밀번호처럼 다룹니다. 이름을 아는 사람은 알림을 볼 수 있습니다. 저장소·채팅에 올리지 않습니다.

시험 알림 보내기:

```sh
. ~/.config/moa/alert.env && curl -d "시험 알림입니다" "https://ntfy.sh/$MOA_NTFY_TOPIC"
```

### 확인·해제

```sh
tail ~/.local/state/moa-health/health.log                 # 상태가 바뀐 기록
launchctl kickstart -k gui/$(id -u)/com.moa.health-check  # 지금 한 번 실행
launchctl bootout gui/$(id -u)/com.moa.health-check       # 해제
```

서버 점검으로 일부러 멈출 때는 알림이 오지 않게 먼저 해제하고, 끝나면 다시 설치합니다.

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.moa.health-check.plist
```

### 알 수 없는 경우

이 확인은 Mac 안에서 실행됩니다. **Mac 자체가 꺼졌거나 인터넷이 끊기면 알림을 보낼 수 없습니다.** 이 경우까지 알려면 NAS 등 다른 기기에서 확인하는 장치를 추가해야 합니다.

## 3. 재부팅·정전 뒤 자동 복구

| 항목                     | 상태               | 설명                                                 |
| ------------------------ | ------------------ | ---------------------------------------------------- |
| 컨테이너 자동 재시작     | 설정됨             | compose의 `restart: unless-stopped`                  |
| Docker Desktop 자동 실행 | 설정됨             | 로그인 직후 상태 확인 작업이 Docker Desktop을 켭니다 |
| 정전 뒤 자동 켜짐        | **직접 설정 필요** | 아래 `pmset` 명령                                    |
| FileVault 잠금 해제      | **사람이 필요**    | 아래 설명                                            |

### 정전 뒤 자동으로 켜지게 하기 (관리자 암호 필요)

```sh
sudo pmset -a autorestart 1
pmset -g | grep autorestart    # 1이면 설정됨
```

### FileVault와 재부팅

Mac은 FileVault가 켜져 있어 재부팅하면 암호를 입력해 잠금을 풀기 전까지 아무것도 실행되지 않습니다. 금융 자료가 있으므로 FileVault는 끄지 않는 것을 권장합니다.

- **계획된 재시작**(macOS 업데이트 등): 아래 명령으로 재시작하면 다음 부팅 한 번은 잠금 화면 없이 로그인까지 진행되어 모아가 자동으로 다시 켜집니다.
  ```sh
  sudo fdesetup authrestart
  ```
- **예상하지 못한 재시작**(정전 등): Mac 앞에서 암호를 입력해 로그인해야 합니다. 로그인하면 상태 확인 작업이 Docker와 모아를 다시 켭니다.
