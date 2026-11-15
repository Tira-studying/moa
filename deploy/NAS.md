# NAS에서 모아 사용하기

x86-64 NAS와 Docker를 대상으로 준비한 구성입니다. 아이폰과 NAS를 같은 Tailscale 개인 네트워크에 연결하고, Tailscale Serve의 HTTPS 주소로 접속합니다. 공유기 포트 포워딩과 Tailscale Funnel(인터넷 공개)은 사용하지 않습니다. Tailscale 연결 외에 모아 로그인 비밀번호도 필요합니다.

클라우드에서 이미지 빌드와 Docker 내 테스트는 검증했습니다. 실제 NAS에는 아직 배포하지 않았으며, 주소도 아직 발급되지 않았습니다. NAS 설정 화면이나 SSH에 접근할 권한이 이 작업 환경에는 없습니다. 아래 명령은 NAS의 터미널에서 실행합니다. 계정 비밀번호·NAS 관리자 비밀번호·Tailscale 로그인 링크는 채팅에 보내지 마세요.

## 1. 아이폰에서 NAS 터미널 접속 준비

처음 한 번은 NAS에서 설치 명령을 실행해야 합니다. GitHub에 저장하는 것만으로 NAS에 자동 설치되지는 않습니다.

1. 아이폰을 NAS와 같은 집의 Wi-Fi에 연결합니다. 아직 Tailscale 설치 전이므로 외부 LTE에서 NAS의 내부 IP로 접속할 수는 없습니다.
2. NAS의 관리 화면에 로그인합니다. 설정에서 SSH/터미널 서비스를 찾아 활성화합니다. 메뉴 이름은 NAS 제조사·버전에 따라 다를 수 있습니다.
3. NAS의 내부 IP와 SSH 포트를 확인합니다. 포트는 보통 22이지만 실제 설정을 따릅니다. 공유기에 SSH 포트 포워딩을 추가하지 않습니다.
4. 아이폰에 SSH 클라이언트(예: Termius)를 설치합니다. Host에는 NAS 내부 IP, Port에는 실제 SSH 포트, Username에는 NAS 관리자 계정을 입력합니다. 비밀번호는 앱의 인증 입력란에서만 입력합니다.
5. 처음 연결할 때 표시되는 호스트 키 지문이 NAS의 키와 맞는지 확인합니다.

NAS 관리 화면에 사용할 수 있는 터미널 기능이 있다면 그것을 사용해도 됩니다. 아래 명령은 아이폰 자체가 아니라 **NAS에 연결된 터미널**에서 실행합니다. 한 블록씩 복사하고 결과를 확인한 뒤 다음 단계로 넘어갑니다.

```sh
docker --version
docker compose version
git --version
```

Docker 권한 오류가 나면 해당 Docker 명령 앞에 `sudo`를 붙이고 NAS에서 비밀번호를 입력합니다. Docker 소켓 권한을 모든 사용자에게 풀지 마세요. `git: command not found`라면 아래 Git 컨테이너 방식을 사용하면 됩니다.

## 2. GitHub에서 내려받기

처음 설치하는 경우입니다. 같은 이름의 기존 폴더가 있으면 덮어쓰거나 삭제하지 말고 다른 설치 폴더를 선택하세요.

```sh
mkdir -p "$HOME/moa-projects"
cd "$HOME/moa-projects"
```

NAS에 Git이 있다면:

```sh
git clone --branch main https://github.com/Tira-studying/moa.git moa
cd moa/deploy
```

NAS에 Git이 없다면 Docker로 Git을 실행합니다:

```sh
docker run --rm -it \
  -v "$PWD":/git -w /git \
  alpine/git clone --branch main https://github.com/Tira-studying/moa.git moa
cd moa/deploy
```

코드는 `$HOME/moa-projects/moa`에 생기고, 실행 설정은 그 안의 `deploy` 폴더에 있습니다. Docker Git을 사용하면 코드 파일 소유자가 root로 보일 수 있지만 이후 업데이트도 동일한 컨테이너 방식으로 할 수 있습니다. 거래 DB는 코드 폴더가 아닌 NAS Docker 데이터 볼륨에 별도로 저장합니다.

### GitHub 인증을 요구하는 경우

공개 저장소는 인증 없이 내려받을 수 있습니다. 비공개 저장소라면 GitHub 계정 비밀번호 대신 저장소 읽기 권한이 있는 인증 수단이 필요합니다. HTTPS 복제는 사용자 이름을 입력하고 Password 프롬프트에 해당 저장소에 대한 `Contents: read` 권한의 fine-grained personal access token을 입력합니다. GitHub 계정 설정의 Developer settings → Personal access tokens에서 만들 수 있습니다. 토큰은 NAS의 숨겨진 인증 프롬프트에만 입력하고, URL·명령문·파일·채팅에 붙여넣지 마세요. 컨테이너 Git 명령은 `-it` 옵션을 유지해야 인증을 입력할 수 있습니다.

NAS에서는 클라우드 작업의 GitHub 인증이 전달되지 않습니다. 권한 오류가 나면 먼저 GitHub에서 본인 계정으로 저장소가 보이는지, 토큰에 이 저장소의 읽기 권한과 유효기간이 있는지 확인합니다. 공개 다운로드를 위해 저장소 공개 범위를 변경할 필요는 없습니다.

## 3. Docker 이미지와 전용 네트워크 준비

지금 위치가 `moa/deploy`인지 `pwd`로 확인합니다. 아래 설정 파일에는 비밀번호를 넣지 않습니다.

```sh
if [ ! -e .env ]; then cp .env.example .env; fi
```

이미 `.env`가 있으면 복사하지 말고 기존 설정을 보존합니다. 아직 주소를 발급받기 전이므로 지금은 예시값으로 둡니다. 이 상태에서 app 컨테이너는 시작하지 않습니다.

```sh
docker compose -f compose.nas.yml build app
```

첫 빌드는 Python·Node·Chromium을 설치하므로 수 분 이상 걸릴 수 있습니다. 완료될 때까지 기다립니다. 실제 명세서·DB·비밀번호는 이미지에 포함하지 않습니다. 함께 제공된 Playwright 공식 seccomp 프로필은 Chromium 샌드박스의 네임스페이스 생성에 필요한 호출을 허용하며, 나머지 시스템 호출 필터는 유지합니다.

```sh
docker compose -f compose.nas.yml up -d tailscale
docker compose -f compose.nas.yml logs --tail 80 tailscale
```

Tailscale 로그의 로그인 안내 링크를 본인 Safari에서 열어 본인 계정으로 NAS를 등록합니다. 링크를 다른 사람이나 채팅에 보내지 마세요. 안내가 없다면:

```sh
docker compose -f compose.nas.yml exec tailscale tailscale up
```

이 구성의 Tailscale은 사용 공간 네트워킹을 사용합니다. 호스트 네트워크·privileged·TUN 장치 권한을 요구하지 않습니다. 기본 개인 네트워크에 접근할 수 있는 다른 계정/장치도 있으므로, Tailscale 공유나 ACL은 본인 기기만 접근하도록 관리하세요.

## 4. HTTPS 주소 발급

```sh
docker compose -f compose.nas.yml exec tailscale tailscale serve --bg --https=443 http://127.0.0.1:8000
```

HTTPS 사용 승인 안내가 나오면 본인 Tailscale 관리 화면에서 MagicDNS와 HTTPS 인증서 기능을 활성화합니다. 도메인 이름은 인증서 투명성 로그에 남을 수 있으므로 기기명에 이름·카드번호 등 개인정보를 넣지 마세요.

명령이 표시한 **실제 `https://…ts.net` 주소**를 `.env`의 `MOA_PUBLIC_ORIGIN` 값으로 지정합니다. NAS의 파일 편집기로 `moa/deploy/.env`를 열어 수정하거나, 터미널에서 다음 명령을 실행한 뒤 발급된 주소를 붙여넣습니다:

```sh
read -r -p "발급받은 HTTPS 주소: " MOA_URL
printf 'MOA_PUBLIC_ORIGIN=%s\n' "$MOA_URL" > .env
unset MOA_URL
```

주소는 비밀번호가 아니지만 본인 전용 주소로 관리하세요. 예시 주소를 그대로 사용하지 않습니다. 경로와 마지막 `/`는 넣지 않습니다. 이후 필요하면 `tailscale serve status`로 주소를 확인할 수 있습니다.

## 5. 앱 시작 및 비밀번호 설정

```sh
docker compose -f compose.nas.yml up -d app
docker compose -f compose.nas.yml exec app python manage.py
```

터미널에서 모아 로그인 비밀번호(12~128자)를 두 번 입력합니다. 입력 내용은 표시되지 않습니다. 카드 명세서 비밀번호와 다르게 설정하세요. 웹에서 누구나 최초 계정을 등록하는 기능은 제공하지 않습니다. 계정 설정 전에는 거래 조회/업로드가 차단됩니다.

비밀번호를 잊었다면 같은 명령으로 재설정할 수 있습니다. 재설정 시 기존 세션은 모두 종료됩니다.

## 6. 보안 브라우저 호환성 확인

```sh
docker compose -f compose.nas.yml exec app node scripts/check-browser.cjs
docker compose -f compose.nas.yml exec -e MOA_BROWSER_TESTS=1 app python -m unittest discover -s tests -v
```

Chromium sandbox OK와 테스트 85개 통과를 확인합니다. 테스트는 가상 명세서·임시 DB만 사용합니다. NAS 커널이나 Docker 정책에 따라 Chromium 자체 샌드박스가 지원되지 않을 수 있습니다. 이 경우 보안 HTML 처리는 중단되며 CSV/XLSX와 로그인은 별개로 동작할 수 있습니다. `--no-sandbox`, privileged, seccomp=unconfined로 우회하지 마세요. 호스트에서 실행 가능한 샌드박스 구성을 별도로 확인해야 합니다.

## 7. 아이폰 접속

1. App Store에서 Tailscale 앱을 설치합니다.
2. NAS와 같은 본인 계정으로 로그인하고 연결을 켭니다.
3. Safari에서 발급받은 실제 HTTPS 주소를 엽니다.
4. 모아 로그인 비밀번호를 입력합니다. Safari의 공유 → 홈 화면에 추가로 바로가기를 만들 수 있습니다.
5. 삼성카드 HTML 파일을 업로드하고 별도의 **명세서 비밀번호**를 일회성 입력합니다.

앱 로그인 세션은 기본 8시간이고, 로그인 화면의 '이 기기에서 로그인 유지'(기본 켜짐)를 켜면 90일이며 쓸 때마다(하루 한 번) 다시 90일로 늘어납니다. 로그아웃하면 해당 세션은 서버에서도 폐기됩니다. 비밀번호를 5번 틀리면 최대 5분간 로그인 시도가 제한됩니다. 아이폰 Tailscale 연결이 꺼져 있으면 주소가 열리지 않는 것이 정상입니다.

## 데이터와 업데이트

거래, 로그인 비밀번호 해시, 서버 세션은 Docker named volume `moa_card-data`에 저장합니다. Tailscale 등록 상태는 `moa_tailscale-state`에 저장합니다. 컨테이너 재생성 후에도 유지되지만 볼륨 삭제 시 사라집니다. `docker compose down -v`를 사용하지 마세요. 비밀번호 원문은 저장하지 않지만 거래 DB는 암호화되지 않으므로 NAS 계정과 백업 접근 권한을 제한하세요.

업데이트 전 DB를 백업합니다. Git이 설치되어 있으면 NAS 터미널에서:

```sh
cd "$HOME/moa-projects/moa"
git pull --ff-only origin main
cd deploy
docker compose -f compose.nas.yml up -d --build app
```

Git 컨테이너로 설치했다면:

```sh
cd "$HOME/moa-projects"
docker run --rm -it -v "$PWD":/git -w /git/moa alpine/git pull --ff-only origin main
cd moa/deploy
```

그다음 프로젝트 deploy 폴더에서:

```sh
docker compose -f compose.nas.yml up -d --build app
```

Tailscale 컨테이너를 재생성했다면 공유 네트워크를 다시 연결하도록 app도 재생성합니다.

```sh
docker compose -f compose.nas.yml up -d --force-recreate app
```

백업은 앱을 중지한 뒤 데이터 볼륨을 NAS의 제한된 백업 공간에 복사하는 방식으로 진행하세요. 실제 명세서와 DB를 Git, Docker 이미지, 공개 저장소에 넣지 마세요.

### 포함된 보안 프로필 출처

`playwright-seccomp.json`은 Microsoft Playwright의 공식 [Docker 프로필](https://github.com/microsoft/playwright/blob/main/utils/docker/seccomp_profile.json)입니다. 라이선스는 `LICENSE.playwright.txt`에 포함했습니다. 프로필을 수정해 샌드박스나 시스템 호출 제한을 무작정 해제하지 마세요.

## 문제 해결

- `Cannot connect to the Docker daemon`: NAS의 Docker 앱이 실행 중인지 확인합니다. 권한 오류라면 `sudo docker ...`를 사용합니다.
- `Repository not found`: 주소 오타 또는 비공개 저장소 인증 문제를 확인합니다. NAS에서 별도 GitHub 인증이 필요할 수 있습니다.
- `MOA_PUBLIC_ORIGIN` 관련 오류: `deploy/.env` 파일이 있는지 확인하고 `.env.example`과 혼동하지 마세요.
- 주소가 안 열림: 아이폰의 Tailscale 연결, NAS의 전원/인터넷, `docker compose -f compose.nas.yml ps`, `tailscale serve status` 순으로 확인합니다.
- `설정된 HTTPS 주소로 접속해 주세요`: `.env` 주소가 Serve에서 발급한 주소와 정확히 일치하는지 확인하고 app을 재생성합니다. 내부 NAS IP로 직접 접속하는 방식은 이 구성에 포함하지 않습니다.
- `아직 로그인 비밀번호가 설정되지 않았습니다`: `docker compose -f compose.nas.yml exec app python manage.py`로 초기 비밀번호를 설정합니다.
- Chromium sandbox 오류: 제공한 seccomp 파일이 `deploy` 폴더에 있는지, Compose를 안내대로 실행했는지 확인합니다. 보안 설정을 꺼서 해결하지 마세요.

오류 도움을 요청할 때는 오류 문구만 공유하고, 비밀번호·토큰·쿠키·명세서 내용·Tailscale 로그인 링크는 가려주세요.

### SSH 배포 키로 업데이트하는 경우

GitHub에 읽기 전용 배포 키를 등록하고 원격 주소를 SSH로 바꾼 설치라면, 업데이트 때 키 볼륨도 연결해야 합니다. 키 생성/등록은 반복하지 않습니다.

```sh
cd "$HOME/moa-projects"
sudo docker run --rm -it -v "$PWD":/git -w /git/moa -v moa-git-ssh:/root/.ssh alpine/git pull --ff-only origin main
cd moa/deploy
sudo docker compose -f compose.nas.yml up -d --build app
```

원격 주소가 HTTPS이면 여전히 HTTPS 인증을 요구합니다. 키가 유효하게 등록되고 GitHub 호스트 지문을 확인한 뒤 SSH 원격 주소(`git@github.com:Tira-studying/moa.git`)를 사용해야 합니다. 개인키/토큰을 채팅에 보내지 마세요.

삼성 명세서에서 `online_required`가 나오면 웹 화면의 `삼성카드 공식 사이트에 연결해 내역과 더보기 불러오기`를 선택하고 다시 엽니다. NAS에서 인터넷 연결이 필요하며 이번 처리 동안에만 공식 HTTPS 연결을 허용합니다. 이 기능은 모아 로그인 쿠키를 삼성카드와 공유하지 않습니다. 최신 오류 진단의 읽기 버전은 9입니다.

신한 PDF 기능을 업데이트할 때는 새 `pdfplumber` 의존성을 설치하도록 `up -d --build --force-recreate app`으로 이미지를 다시 만듭니다. 웹 화면의 `신한카드 (PDF)`에서 원본과 비밀번호를 입력하며, 실제 비밀번호를 GitHub·채팅·터미널 명령 인자로 전달하지 않습니다.

현대 보안 HTML은 웹 화면의 `현대카드 (HTML)`에서 읽습니다. 삼성 공식 사이트 연결 옵션은 사용할 수 없습니다. 업데이트 후 브라우저를 새로고침하고 원본 파일을 다시 선택하세요. 실제 파일의 거래 추출이 안 되면 오류 문구와 제공되는 가상 구조 진단 파일을 확인합니다.

KB XLSX는 상단 CSV/XLSX 업로드에서 KB국민을 선택합니다. 제공된 2026년 명세서는 제목 자동 찾기를 켜고 적용 연도 2026, 미리보기의 마지막 거래 행 5를 지정합니다. 삼성 HTML 읽기 오류와는 별도 경로입니다.

Gmail 자동 수집과 공통 암호는 선택 기능입니다. [GMAIL.md](GMAIL.md)의 Gmail 계정·라벨·비밀 파일 설정 후 `--profile mail`로 수집 서비스를 시작합니다.

## 공동관리자와 함께 사용

서로 다른 Tailscale 계정에서도 앱용 `moa-card` 장치만 공유하고 모아에는 별도 계정으로 로그인할 수 있습니다. NAS 본체나 SSH 권한을 공유하지 않습니다. 기존 소유자의 로그인 아이디는 `owner`이며 비밀번호는 유지됩니다. 모아 상단 ‘계정’에서 공동관리자를 관리합니다. 설정·권한·접근 해제·동시 수정 안내는 [공동 관리 설치 안내](SHARING.md)를 참고하세요.
