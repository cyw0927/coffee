from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

MENUS = {
    "M01": "아이스아메리카노",
    "M02": "아이스라떼",
    "M03": "카페라떼",
    "M04": "말차라떼",
    "M05": "아이스티",
}

KST = timezone(timedelta(hours=9), name="KST")
TOKEN_TTL_SECONDS = 300
CHALLENGE_TTL_SECONDS = 60
MAX_NAME_LENGTH = 20

# 서버 재시작 시 기존 기기 토큰이 무효가 되어도 괜찮으므로, 비밀값 미설정 시 매번 새 키를 생성한다.
TOKEN_SECRET = os.getenv("DEVICE_TOKEN_SECRET", "").encode("utf-8") or secrets.token_bytes(32)
SLACK_MOCK = os.getenv("SLACK_MOCK", "0") == "1"
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()
PORT = int(os.getenv("PORT", "8000"))

challenges: dict[str, float] = {}
processed_orders: dict[str, tuple[int, dict[str, Any]]] = {}
mock_slack_messages: list[str] = []
order_lock = asyncio.Lock()


class DeviceProof(BaseModel):
    challenge_id: str = Field(min_length=16, max_length=128)
    max_touch_points: int = Field(ge=1, le=20)
    coarse_pointer: bool
    motion: dict[str, Any] | None = None
    orientation: dict[str, Any] | None = None


class OrderRequest(BaseModel):
    order_id: str = Field(min_length=36, max_length=36)
    name: str = Field(max_length=100)
    menu_code: str = Field(min_length=1, max_length=20)
    device_token: str = Field(min_length=20, max_length=2048)


def get_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def is_mobile_request(request: Request) -> bool:
    ua = request.headers.get("user-agent", "").lower()
    ch_mobile = request.headers.get("sec-ch-ua-mobile")

    # Chromium 계열이 명시적으로 데스크톱이라고 알려주면 즉시 차단한다.
    if ch_mobile == "?0":
        return False

    mobile_markers = ("android", "iphone", "ipad", "ipod", "mobile")
    return any(marker in ua for marker in mobile_markers)


def blocked_page() -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>접속 제한</title>
</head>
<body style="font-family:system-ui,sans-serif;padding:40px;line-height:1.6">
  <h1>📱 스마트폰에서 접속해 주세요</h1>
  <p>이 주문 페이지는 실제 모바일 기기에서만 사용할 수 있습니다.</p>
</body>
</html>""",
        status_code=403,
    )


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def ua_hash(request: Request) -> str:
    ua = request.headers.get("user-agent", "")
    return hashlib.sha256(ua.encode("utf-8")).hexdigest()[:24]


def issue_device_token(request: Request) -> str:
    payload = {
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
        "ua": ua_hash(request),
        "nonce": secrets.token_hex(8),
    }
    payload_part = b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(TOKEN_SECRET, payload_part.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_part}.{b64url_encode(signature)}"


def verify_device_token(token: str, request: Request) -> bool:
    try:
        payload_part, signature_part = token.split(".", 1)
        expected = hmac.new(TOKEN_SECRET, payload_part.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, b64url_decode(signature_part)):
            return False
        payload = json.loads(b64url_decode(payload_part))
        if int(payload.get("exp", 0)) < int(time.time()):
            return False
        if payload.get("ua") != ua_hash(request):
            return False
        return True
    except (ValueError, TypeError, json.JSONDecodeError, base64.binascii.Error):
        return False


def clean_name(raw: str) -> str:
    name = " ".join(raw.strip().split())
    if not name:
        raise ValueError("이름을 입력해 주세요.")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"이름은 {MAX_NAME_LENGTH}자 이하로 입력해 주세요.")
    return name


def escape_for_slack(name: str) -> str:
    # Slack 링크/멘션 문법을 무력화한다.
    return (
        name.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("@", "＠")
    )


def has_real_sensor_values(proof: DeviceProof) -> bool:
    def numeric(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    motion = proof.motion or {}
    orientation = proof.orientation or {}

    motion_values = [motion.get("x"), motion.get("y"), motion.get("z")]
    orientation_values = [orientation.get("alpha"), orientation.get("beta"), orientation.get("gamma")]

    return any(numeric(v) for v in motion_values + orientation_values)


async def send_to_slack(message: str) -> None:
    if SLACK_MOCK:
        mock_slack_messages.append(message)
        print(f"[SLACK_MOCK] {message}")
        return

    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL이 설정되지 않았습니다.")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(SLACK_WEBHOOK_URL, json={"text": message})
    except httpx.HTTPError as exc:
        raise RuntimeError("Slack 전송 중 네트워크 오류가 발생했습니다.") from exc

    if response.status_code != 200:
        raise RuntimeError(f"Slack 전송 실패 (HTTP {response.status_code})")


@asynccontextmanager
async def lifespan(_: FastAPI):
    mode = "MOCK" if SLACK_MOCK else "REAL"
    print("=" * 58)
    print(f"커피 주문 서버 시작 · Slack 모드: {mode}")
    print(f"스마트폰 접속 주소: http://{get_lan_ip()}:{PORT}")
    print("센서 API가 HTTP에서 제한되면 README의 HTTPS/ngrok 방법을 사용하세요.")
    print("=" * 58)
    yield


app = FastAPI(title="모바일 커피 주문", lifespan=lifespan)


@app.middleware("http")
async def no_store(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not is_mobile_request(request):
        return blocked_page()
    html = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/menus")
async def get_menus(request: Request):
    if not is_mobile_request(request):
        return JSONResponse({"detail": "스마트폰에서 접속해 주세요."}, status_code=403)
    return [{"code": code, "name": name} for code, name in MENUS.items()]


@app.post("/api/device/challenge")
async def create_device_challenge(request: Request):
    if not is_mobile_request(request):
        return JSONResponse({"detail": "스마트폰에서 접속해 주세요."}, status_code=403)

    now = time.time()
    expired = [key for key, exp in challenges.items() if exp < now]
    for key in expired:
        challenges.pop(key, None)

    challenge_id = secrets.token_urlsafe(24)
    challenges[challenge_id] = now + CHALLENGE_TTL_SECONDS
    return {"challenge_id": challenge_id, "expires_in": CHALLENGE_TTL_SECONDS}


@app.post("/api/device/verify")
async def verify_device(proof: DeviceProof, request: Request):
    if not is_mobile_request(request):
        return JSONResponse({"detail": "스마트폰에서 접속해 주세요."}, status_code=403)

    expires_at = challenges.pop(proof.challenge_id, None)
    if expires_at is None or expires_at < time.time():
        return JSONResponse({"detail": "기기 인증 요청이 만료되었습니다. 다시 시도해 주세요."}, status_code=403)
    if not proof.coarse_pointer or proof.max_touch_points <= 0:
        return JSONResponse({"detail": "터치 기반 모바일 기기로 확인되지 않았습니다."}, status_code=403)
    if not has_real_sensor_values(proof):
        return JSONResponse({"detail": "실제 기기 센서 값을 확인하지 못했습니다."}, status_code=403)

    return {"device_token": issue_device_token(request), "expires_in": TOKEN_TTL_SECONDS}


@app.post("/api/order")
async def order(payload: OrderRequest, request: Request):
    if not is_mobile_request(request):
        return JSONResponse({"detail": "스마트폰에서 접속해 주세요."}, status_code=403)
    if not verify_device_token(payload.device_token, request):
        return JSONResponse({"detail": "기기 인증이 없거나 만료되었습니다."}, status_code=403)

    import uuid

    try:
        order_uuid = uuid.UUID(payload.order_id, version=4)
        if str(order_uuid) != payload.order_id.lower():
            raise ValueError
    except ValueError:
        return JSONResponse({"detail": "잘못된 주문 ID입니다."}, status_code=400)

    try:
        display_name = clean_name(payload.name)
        slack_name = escape_for_slack(display_name)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    menu_name = MENUS.get(payload.menu_code)
    if menu_name is None:
        return JSONResponse({"detail": "존재하지 않는 메뉴 코드입니다."}, status_code=400)

    # 한 프로세스 안에서 동일 ID의 동시 요청까지 1회만 Slack으로 보내도록 잠근다.
    async with order_lock:
        previous = processed_orders.get(payload.order_id)
        if previous is not None:
            previous_status, previous_body = previous
            if previous_status == 200:
                return previous_body
            return JSONResponse(previous_body, status_code=previous_status)

        ordered_at = datetime.now(KST).strftime("%H:%M")
        message = f"{slack_name} | {payload.menu_code} | {menu_name} | {ordered_at}"

        try:
            await send_to_slack(message)
        except RuntimeError as exc:
            result = {
                "ok": False,
                "order_id": payload.order_id,
                "detail": str(exc),
            }
            processed_orders[payload.order_id] = (502, result)
            return JSONResponse(result, status_code=502)

        result = {
            "ok": True,
            "order_id": payload.order_id,
            "name": display_name,
            "menu_code": payload.menu_code,
            "menu_name": menu_name,
            "ordered_at": ordered_at,
            "status": "주문 완료",
        }
        processed_orders[payload.order_id] = (200, result)
        return result


@app.get("/api/debug/mock-messages")
async def mock_messages(request: Request):
    if not SLACK_MOCK:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    if not is_mobile_request(request):
        return JSONResponse({"detail": "스마트폰에서 접속해 주세요."}, status_code=403)
    return {"count": len(mock_slack_messages), "messages": mock_slack_messages}


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
