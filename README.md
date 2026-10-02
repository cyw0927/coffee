# ☕ 한 잔 — 모바일 커피 주문

실제 스마트폰에서 이름과 메뉴를 선택해 Slack **#커피주문** 채널로 주문하는 실습 앱입니다. Python + Flask 단일 서버와 순수 HTML/CSS/JS로 구성했습니다.

완성된 앱은 `main` 브랜치에 있습니다.

## 빠른 실행

Python 3.10 이상이 필요합니다. Windows에서는 **`start.bat`를 더블클릭**하면 가상환경 생성, 의존성 설치, `.env` 준비와 실행을 자동으로 합니다. 설치된 Python이 없으면 Codex의 번들 Python도 찾아 사용합니다.

직접 실행하려면:

```sh
git clone https://github.com/cyw0927/coffee.git
cd coffee
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe app.py
```

macOS / Linux:

```sh
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python app.py
```

가상환경을 활성화했다면 `python app.py` 한 줄로 실행합니다. `0.0.0.0:8000`에 바인딩하며 콘솔에 다음 주소를 출력합니다.

```text
스마트폰 접속 주소: http://192.168.x.x:8000
```

## 실제 휴대폰으로 열기 — HTTPS 필수

노트북과 스마트폰을 같은 Wi-Fi에 연결하고 콘솔의 LAN 주소를 휴대폰 Safari 또는 Chrome에서 열 수 있습니다. Windows 방화벽 알림이 나타나면 **개인 네트워크**에서 Python 접속을 허용하세요. 게스트 Wi-Fi의 기기 간 접속 차단, VPN, 다른 네트워크 어댑터 때문에 연결이 안 되면 콘솔의 다른 LAN IP를 시도하세요.

**일반 LAN HTTP에서는 최신 브라우저가 움직임 센서 접근을 차단하므로 페이지는 열려도 주문 인증은 불가능할 수 있습니다. 최종 센서 인증과 주문 테스트는 HTTPS 주소에서 하세요.** 이 조건을 맞추는 간단한 방법은 노트북 서버를 ngrok으로 연결하는 것입니다. 앱 실행은 계속 노트북에서 이루어집니다.

1. [ngrok 공식 사이트](https://ngrok.com/download)에서 설치하고 무료 계정의 인증 토큰을 등록합니다.
2. 서버를 켜 둔 상태에서 별도 터미널을 열고 실행합니다.

```sh
ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
ngrok http 8000
```

3. ngrok에 표시된 **HTTPS 접속 주소**를 실제 스마트폰의 **Safari / Chrome**에서 엽니다. 최초 ngrok 안내가 뜨면 Visit Site를 누릅니다. Slack 안의 내장 브라우저보다 기본 브라우저 사용을 권장합니다.
4. **스마트폰 인증하기**를 누릅니다. iPhone은 움직임 및 방향 권한을 허용하고, 휴대폰을 살짝 기울입니다. Android는 Chrome의 사이트 설정에서 움직임 센서가 허용되어 있어야 합니다.
5. 이름과 메뉴를 선택해 주문합니다. 인증 유효기간은 5분이고 만료되면 다시 인증합니다.

로컬 신뢰 인증서를 설치한 HTTPS 구성도 가능하지만 이 프로젝트에는 포함하지 않았습니다. ngrok을 쓰면 같은 Wi-Fi가 아니어도 접속할 수 있습니다. ngrok 주소는 서버 실행 중에만 유효하며 공개된 주소이므로 필요한 사람에게만 전달하세요.

## 실제 Slack 주문 설정

`.env`를 편집합니다. Webhook은 반드시 **#커피주문 채널에 연결된 Incoming Webhook**을 사용하세요. Incoming Webhook의 전송 채널은 Slack에서 생성할 때 정해집니다.

```dotenv
SLACK_WEBHOOK_URL=여기에_실제_Slack_Webhook_HTTPS_URL
SLACK_MOCK=0
MENU_SOURCE=slack
SLACK_BOT_TOKEN=여기에_채널_읽기_권한이_있는_Bot_Token
SLACK_MENU_MESSAGE_URL=여기에_메뉴_공지_메시지_링크
PORT=8000
TOKEN_SECRET=
```

URL을 변경한 후 서버를 다시 실행합니다. Webhook URL은 `.env`에서만 읽고 HTML/JS/API 응답에 포함하지 않습니다. `.env`는 Git에서 제외합니다. URL을 README나 GitHub에 붙여 넣지 마세요.

`SLACK_MOCK=1`이면 **Slack 전송 없이** 콘솔과 로컬 `data/orders.sqlite3`의 `mock_messages` 테이블에 기록합니다. 화면에도 **연습 모드 / 연습 주문 완료 (미전송)**를 표시합니다. **실습의 최종 성공 조건은 반드시 `SLACK_MOCK=0`에서 실제 휴대폰 주문이 Slack에 도착하는 것입니다.**

## 메뉴는 Slack 공지에서 읽기

**메뉴 코드와 메뉴명은 서버 코드에 하드코딩하지 않습니다.** `MENU_SOURCE=slack`에서 서버가 Slack API로 지정한 공지 메시지를 읽고, 메시지 안의 JSON 배열을 메뉴 목록으로 사용합니다. 페이지 표시와 주문 검증 모두 이 목록을 기준으로 하며, 클라이언트가 보낸 메뉴명은 무시합니다. 공지를 수정하면 최대 1분 안에 다음 조회에서 반영됩니다. 서버를 재시작하면 캐시가 초기화됩니다.

`.env`에 다음 두 값을 설정하세요.

- `SLACK_MENU_MESSAGE_URL`: **메뉴 공지 메시지**의 ⋮ 메뉴 → **링크 복사**로 얻은 링크. 채널 본문 공지를 사용하며, 댓글에 있는 공지는 지원하지 않습니다.
- `SLACK_BOT_TOKEN`: 해당 워크스페이스 Slack 앱의 **Bot User OAuth Token**. 이 값은 Webhook URL과 다릅니다. 공개 채널에는 `channels:history`, 비공개 채널에는 `groups:history` 읽기 권한이 필요하며 봇을 메뉴 공지 채널에 초대해야 합니다. 토큰은 `.env`에만 넣고 GitHub나 채팅에 올리지 마세요.

봇 토큰이 없다면 Slack 앱 담당자에게 요청하거나, 권한이 있는 계정으로 [Slack 앱 관리](https://api.slack.com/apps)에서 앱을 만든 뒤 **OAuth & Permissions → Bot Token Scopes**에 필요한 읽기 권한을 추가하고 워크스페이스에 설치합니다. 설치 후 표시되는 Bot User OAuth Token을 사용하세요. 워크스페이스 정책에 따라 관리자 승인이 필요할 수 있습니다. **주문용 Incoming Webhook은 메시지 전송용이어서 공지 조회에 사용할 수 없습니다.**

공지 메시지는 아래처럼 **닫는 `]`까지 있는 완전한 JSON 배열**이어야 합니다. 일반 안내 문구나 코드 블록으로 둘러싸여 있어도 배열을 찾아 읽습니다. 공지에 메뉴 배열이 여러 개 있거나, 코드가 중복되거나, JSON이 잘못되면 메뉴를 표시하지 않고 새 주문을 거부합니다.

```json
[
  {"code": "M01", "name": "아이스아메리카노"},
  {"code": "M02", "name": "아이스라떼"},
  {"code": "M03", "name": "카페라떼"},
  {"code": "M04", "name": "말차라떼"},
  {"code": "M05", "name": "아이스티"}
]
```

조회 타임아웃은 5초입니다. Slack 요청 제한을 줄이기 위해 결과와 오류를 60초 캐시하며 동시에 들어오는 조회는 하나로 합칩니다. 캐시가 만료된 뒤 조회가 실패하면 오래된 메뉴나 연습 메뉴로 대체하지 않고 503 오류를 보여 줍니다. 읽기 토큰이나 공지 링크는 화면·API 응답에 노출하지 않습니다. 공지 변경 뒤 이미 처리된 주문 ID를 조회하면 당시 주문 결과를 그대로 반환하고 재전송하지 않습니다.

### Slack 연결 없이 연습하기

아직 읽기 토큰이 없다면 **연습 모드에서만** 별도 JSON 파일을 읽도록 설정할 수 있습니다.

```dotenv
SLACK_MOCK=1
MENU_SOURCE=file
```

이때만 `menus.json`에서 메뉴를 읽으며 파일을 수정하면 다음 페이지 표시·새 주문에 즉시 반영됩니다. 이 파일은 연결 없이 검증하기 위한 데이터이고 실제 Slack 공지를 읽었다는 의미가 아닙니다. `SLACK_MOCK=0` 실제 주문에서는 `MENU_SOURCE=file`을 거부합니다. 실제 과제 테스트는 반드시 `MENU_SOURCE=slack`을 사용하세요.

현재 `menus.json`의 연습 메뉴는 아래와 같습니다. 화면의 컵 그림은 장식용입니다.

| 메뉴 코드 | 메뉴명 |
| --- | --- |
| M01 | 아이스아메리카노 |
| M02 | 아이스라떼 |
| M03 | 카페라떼 |
| M04 | 말차라떼 |
| M05 | 아이스티 |

Slack 메시지 예시:

```text
홍길동 | M01 | 아이스아메리카노 | 14:25
```

시간은 운영체제의 시간대와 무관하게 KST를 사용합니다. 메뉴명은 서버가 코드로 조회합니다. 이름은 공백 제거 후 1~20자만 받고 줄바꿈·제어문자·`|`를 거부합니다. Slack fallback 텍스트의 `& < >`를 이스케이프하고 화면 표시용 블록은 `plain_text`로 보내 멘션과 서식을 방지합니다.

## 모바일 검증과 한계

- 페이지·정적 파일·주문 API 모두 Android **Mobile** 또는 iPhone UA만 허용합니다. `Sec-CH-UA-Mobile: ?0`이면 거부합니다. 모바일 헤더가 없는 iPhone Safari는 UA로 검사합니다.
- 클라이언트는 터치 포인트, `pointer: coarse`, HTTPS, 실제 브라우저 센서 이벤트를 검사합니다. 최소 3개 숫자 샘플, 100ms 이상 간격, 값 변화를 요구합니다. `null`, 고정값, 스크립트로 직접 만든 이벤트는 통과하지 않습니다.
- 서버도 센서 증명 값과 60초 단발 챌린지를 검증합니다. 통과하면 세션 쿠키 및 UA에 묶인 5분 유효 서명 토큰을 발급합니다. 주문 API는 유효 토큰 없이 항상 403입니다. 토큰·인증 우회용 테스트 모드는 없습니다. `SLACK_MOCK`도 센서 인증을 생략하지 않습니다.
- 기본 PC 브라우저와 화면 크기만 바꾸는 모바일 에뮬레이션은 차단됩니다. 터치/UA를 에뮬레이션해도 기본적으로 센서 값이 없어서 인증에 실패합니다.
- **웹 브라우저 정보로 실제 스마트폰을 완벽하게 증명할 수는 없습니다.** UA, 헤더, 터치, 서버에 제출하는 센서 값은 조작할 수 있고, 개발자 도구에서 센서까지 설정하거나 별도 클라이언트로 증명을 위조하면 우회할 수 있습니다. 서명 토큰은 서버가 발급했음을 보장하지만 센서가 실제 기기에서 왔음을 보증하지 않습니다. 절대적인 PC 차단에는 네이티브 앱과 기기 인증 같은 별도 설계가 필요합니다.
- 센서가 없거나 권한이 차단된 실제 휴대폰도 거부합니다. 이 경우 HTTPS·권한·Safari/Chrome 사용을 확인하세요. 센서 검증을 없애는 우회 설정은 제공하지 않습니다.

## 중복 클릭·재시도·Slack 오류

버튼은 클릭 즉시 비활성화되고 UUID v4 주문 ID를 브라우저에서 생성합니다. 동일 ID는 SQLite에 먼저 등록해 동시에 요청해도 한 번만 전송합니다. 서버 재시작 뒤에도 완료 결과는 유지합니다. 같은 ID로 다른 이름/메뉴를 보내면 409입니다. 네트워크가 끊겨도 브라우저에 저장된 ID로 **결과 확인하기**를 하며, 재전송하지 않습니다. 결과가 확인될 때까지 입력을 고정합니다.

Slack은 5초 타임아웃과 **HTTP 200 + `ok`** 응답을 요구합니다. 실패는 주문 완료로 표시하지 않습니다. non-200은 오류를 표시하고 새 주문을 허용합니다. 타임아웃/연결 오류나 전송 중 서버 종료는 Slack이 받았는지 불확실하므로 같은 ID의 전송을 다시 시도하지 않습니다. 먼저 Slack 채널을 확인하세요. 오래된 처리 중 주문은 30초 뒤 `unknown`으로 표시합니다.

Slack에는 전송 ID를 통한 원자적인 중복 제거 기능이 없으므로 전송 성공 직후 서버가 종료되면 결과를 확정할 수 없습니다. 이 구현은 **동일 ID를 재전송하지 않는 쪽**을 선택합니다. 사용자가 새 ID로 직접 새 주문을 만들거나 로컬 DB를 지우면 이 중복 방지는 적용되지 않습니다. `data/`에는 이름과 주문 결과가 로컬로 남으며 Git에는 올라가지 않습니다.

## 검증

가상환경 Python으로 실행합니다. 실제 Webhook을 설정했더라도 아래 검증은 임시 DB와 mock/stub만 사용하므로 **실제 Slack에는 보내지 않습니다.**

```sh
python -m unittest discover -s tests -p "test_*.py" -v
python tests/curl_checks.py
```

두 번째 명령은 실제 임시 HTTP 서버를 켜고 **curl**로 다음을 확인합니다.

1. PC UA 페이지와 주문 API → 403
2. 모바일 UA지만 토큰 없음 → 403
3. 공백 이름 → 400
4. 없는 메뉴 코드 → 400
5. 같은 주문 ID 2번 → 동일 결과 / mock 전송 기록 1개

자동 검증의 센서 샘플은 인위적인 테스트 데이터입니다. 실제 스마트폰 인증 성공이나 실제 Slack 수신을 입증하지 않습니다. **최종 검증은 본인의 휴대폰으로 직접 진행하세요.**

실습용 단일 프로세스 서버이며 Flask 디버그 모드는 꺼져 있습니다. 여러 서버 프로세스로 확장하거나 공개 상용 서비스로 운영하는 구성은 범위 밖입니다.

참고: [센서 권한과 HTTPS](https://developer.mozilla.org/en-US/docs/Web/API/DeviceMotionEvent/requestPermission_static), [Slack 메뉴 조회](https://docs.slack.dev/reference/methods/conversations.history/), [Slack Incoming Webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/), [ngrok 빠른 시작](https://ngrok.com/docs/gateway/endpoints/agent-cli-quickstart).
