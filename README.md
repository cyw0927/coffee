# ☕ 모바일 커피 주문 웹앱

실제 스마트폰에서 메뉴를 골라 Slack `#커피주문` 채널로 주문을 보내는 FastAPI 실습 앱입니다.

## 메뉴

| 코드 | 메뉴명 |
|---|---|
| M01 | 아이스아메리카노 |
| M02 | 아이스라떼 |
| M03 | 카페라떼 |
| M04 | 말차라떼 |
| M05 | 아이스티 |

## 1. 설치

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

`.env`에서 실제 주문 때는 다음처럼 설정합니다.

```env
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
SLACK_MOCK=0
DEVICE_TOKEN_SECRET=충분히-긴-랜덤-문자열
PORT=8000
```

Webhook URL은 `.env`에만 두며 Git에 올리지 않습니다.

## 2. 실행

```bash
python app.py
```

서버는 `0.0.0.0`에 바인딩되고 콘솔에 다음 형식으로 주소가 표시됩니다.

```text
스마트폰 접속 주소: http://192.168.x.x:8000
```

노트북과 스마트폰을 **같은 Wi-Fi**에 연결한 뒤 이 주소를 스마트폰 브라우저에서 엽니다. Windows 방화벽이 물으면 Python의 사설 네트워크 접근을 허용합니다.

## 3. 모바일 기기 인증

다중 방어를 사용합니다.

- 서버: 모바일 User-Agent가 아니면 페이지/API 403
- 서버: `Sec-CH-UA-Mobile: ?0`이면 403
- 브라우저: `navigator.maxTouchPoints > 0`
- 브라우저: `(pointer: coarse)` 확인
- 브라우저: `devicemotion` 또는 `deviceorientation`의 실제 숫자값 확인
- 서버: 1회용 challenge 확인 후 5분짜리 HMAC 서명 토큰 발급
- 주문 API: 유효한 기기 토큰 없으면 403

### iPhone / 센서가 동작하지 않을 때

최신 브라우저는 기기 센서 API를 **보안 컨텍스트(HTTPS)** 에서만 허용할 수 있습니다. 같은 Wi-Fi의 `http://192.168...`에서 인증이 실패하면 HTTPS 터널을 사용합니다.

예시(ngrok 설치 후):

```bash
ngrok http 8000
```

ngrok이 보여 주는 `https://...` 주소를 스마트폰에서 열면 됩니다. Webhook 비밀값은 여전히 서버 `.env`에만 있습니다.

## 4. 주문 검증

서버에서 다시 검증합니다.

- 이름: 공백 거부, 20자 제한, Slack 멘션/링크용 특수문자 이스케이프
- 메뉴: 클라이언트 메뉴명을 신뢰하지 않고 `menu_code`로 서버 상수에서 조회
- Slack: 5초 타임아웃, HTTP 200이 아니면 실패 처리
- 중복 방지: 브라우저가 UUID v4 주문 ID 생성 + 서버가 처리 결과 캐시. 같은 ID는 Slack 재전송 없이 기존 결과 반환

Slack 메시지 형식:

```text
이름 | 메뉴코드 | 메뉴명 | HH:MM
```

시간은 KST입니다.

## 5. Mock 모드

실제 Slack 전송 전 `.env`에서:

```env
SLACK_MOCK=1
```

Mock 모드는 Slack 대신 콘솔과 서버 메모리에 메시지를 기록합니다.

서버를 켠 뒤 Git Bash/WSL 등 bash 환경에서:

```bash
bash scripts/curl_checks.sh
```

검증 항목:

1. PC UA → 403
2. 모바일 UA지만 기기 토큰 없음 → 403
3. 이름 빈값 → 400
4. 없는 메뉴 코드 → 400
5. 같은 주문 ID 두 번 → 주문 응답은 재사용되고 mock Slack 기록은 1회만 증가

`curl`의 센서 proof는 **서버 검증 로직을 테스트하기 위한 합성값**입니다. 실제 최종 시험은 스마트폰 브라우저의 센서 이벤트로 진행해야 합니다.

## 한계

브라우저만으로 “진짜 스마트폰”을 완벽하게 증명하는 것은 불가능합니다. User-Agent, Client Hint, 터치 정보와 JavaScript 센서 이벤트는 충분한 기술 지식이 있으면 위조될 수 있습니다. 이 앱은 일반적인 PC 브라우저 및 개발자 도구의 단순 모바일 에뮬레이션을 여러 신호로 막는 실습용 방어이며, 보안 하드웨어 기반 기기 증명(attestation)은 아닙니다.
