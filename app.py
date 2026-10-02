"""휴대폰 전용 커피 주문 서버. 실행: python app.py"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import UUID

from dotenv import dotenv_values
from flask import Flask, jsonify, make_response, render_template, request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException
from menu_source import MenuSourceError, SlackMenuSource, read_mock_menus

ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))
TOKEN_TTL = 300
CHALLENGE_TTL = 60
SESSION_TTL = 86400
SLACK_TIMEOUT = 5

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def mobile_request() -> bool:
    ua = request.headers.get("User-Agent", "")
    mobile_ua = "iPhone" in ua or ("Android" in ua and "Mobile" in ua)
    return mobile_ua and request.headers.get("Sec-CH-UA-Mobile", "").strip() != "?0"


def sensor_proof_valid(body: dict) -> bool:
    if body.get("coarse_pointer") is not True or body.get("secure_context") is not True:
        return False
    points = body.get("max_touch_points")
    if type(points) is not int or not 1 <= points <= 100:
        return False
    if body.get("sensor_kind") not in ("motion", "orientation"):
        return False
    samples = body.get("samples")
    if not isinstance(samples, list) or not 3 <= len(samples) <= 12:
        return False
    previous_t = -1
    for sample in samples:
        if not isinstance(sample, dict):
            return False
        stamp, values = sample.get("t"), sample.get("values")
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= stamp <= 15000 or stamp <= previous_t:
            return False
        previous_t = stamp
        if not isinstance(values, list) or len(values) != 3:
            return False
        if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 10000 for v in values):
            return False
    elapsed = samples[-1]["t"] - samples[0]["t"]
    changed = any(max(s["values"][axis] for s in samples) - min(s["values"][axis] for s in samples) >= 0.02 for axis in range(3))
    return elapsed >= 100 and changed


def send_slack(webhook: str, text: str, display_text: str) -> tuple[int, dict]:
    # Top-level fallback is escaped; the visible block is plain text, so it cannot mention anyone.
    payload = json.dumps({"text": text, "mrkdwn": False,
                          "blocks": [{"type": "section", "text": {"type": "plain_text", "text": display_text, "emoji": False}}]}, ensure_ascii=False).encode("utf-8")
    outbound = Request(webhook, data=payload, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with build_opener(NoRedirect()).open(outbound, timeout=SLACK_TIMEOUT) as response:
            if response.status == 200 and response.read(128).strip() == b"ok":
                return 200, {"delivery_status": "sent"}
            return 502, {"delivery_status": "failed", "error": "Slack이 주문을 받지 못했습니다. Webhook 설정을 확인한 후 다시 주문해 주세요."}
    except HTTPError:
        return 502, {"delivery_status": "failed", "error": "Slack 전송에 실패했습니다. Webhook과 채널 설정을 확인해 주세요."}
    except (TimeoutError, URLError, OSError):
        # A timeout does not prove non-delivery. Never resend this ID automatically.
        return 504, {"delivery_status": "unknown", "error": "Slack 응답을 확인하지 못했습니다. 중복 주문을 막기 위해 재전송하지 않습니다. #커피주문 채널을 먼저 확인해 주세요."}


def create_app(test_config: dict | None = None) -> Flask:
    env = dotenv_values(ROOT / ".env")
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=env.get("TOKEN_SECRET") or secrets.token_hex(32),
        # Webhook is deliberately read only from .env, never sent to the browser.
        SLACK_WEBHOOK_URL=env.get("SLACK_WEBHOOK_URL") or "",
        SLACK_MOCK=os.getenv("SLACK_MOCK", env.get("SLACK_MOCK") or "0") == "1",
        MENU_SOURCE=env.get("MENU_SOURCE") or "slack",
        SLACK_BOT_TOKEN=env.get("SLACK_BOT_TOKEN") or "",
        SLACK_MENU_MESSAGE_URL=env.get("SLACK_MENU_MESSAGE_URL") or "",
        MENU_FILE=ROOT / "menus.json",
        DATABASE=ROOT / "data" / "orders.sqlite3",
        MAX_CONTENT_LENGTH=8192,
    )
    if test_config:
        app.config.update(test_config)
    if app.config["MENU_SOURCE"] not in ("slack", "file"):
        raise ValueError("MENU_SOURCE를 slack 또는 file로 설정해 주세요.")
    if app.config["MENU_SOURCE"] == "file" and not app.config["SLACK_MOCK"] and not app.config["TESTING"]:
        raise ValueError("실제 주문은 MENU_SOURCE=slack으로 설정해 Slack 공지에서 메뉴를 읽어야 합니다.")
    slack_menus = SlackMenuSource(app.config["SLACK_BOT_TOKEN"], app.config["SLACK_MENU_MESSAGE_URL"])

    def load_menus():
        if app.config["MENU_SOURCE"] == "file":
            return read_mock_menus(app.config["MENU_FILE"])
        return slack_menus.read()

    @app.errorhandler(MenuSourceError)
    def menu_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=str(error)), 503
        return render_template("menu_error.html", error=str(error)), 503
    if not app.config["SLACK_MOCK"] and app.config["SLACK_WEBHOOK_URL"]:
        parsed = urlsplit(app.config["SLACK_WEBHOOK_URL"])
        if parsed.scheme != "https" or parsed.netloc != "hooks.slack.com" or not parsed.path.startswith("/services/") or parsed.query or parsed.fragment:
            raise ValueError(".env의 SLACK_WEBHOOK_URL에 올바른 Slack Incoming Webhook HTTPS URL을 넣어 주세요.")

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    signer = URLSafeTimedSerializer(app.config["SECRET_KEY"])
    challenges: dict[str, tuple[str, float]] = {}
    challenge_lock = threading.Lock()

    @contextmanager
    def connect():
        con = sqlite3.connect(app.config["DATABASE"], timeout=10)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
            created REAL NOT NULL, http_status INTEGER, result TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS mock_messages (
            order_id TEXT PRIMARY KEY, text TEXT NOT NULL)""")

    def session_id():
        try:
            value = signer.loads(request.cookies.get("coffee_session", ""), salt="session", max_age=SESSION_TTL)
            return value if isinstance(value, str) else None
        except BadSignature:
            return None

    def ua_hash():
        return hashlib.sha256(request.headers.get("User-Agent", "").encode()).hexdigest()

    def token_valid():
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        try:
            token = signer.loads(auth[7:], salt="mobile", max_age=TOKEN_TTL)
            return token.get("sid") == session_id() and token.get("ua") == ua_hash() and bool(token.get("sid"))
        except (BadSignature, AttributeError):
            return False

    def json_body():
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else None

    def previous_order(con, existing, order_id, fingerprint):
        if existing["fingerprint"] != fingerprint:
            return jsonify(error="같은 주문 ID로 다른 내용을 주문할 수 없습니다."), 409
        if existing["state"] == "processing":
            if time.time() - existing["created"] <= 30:
                return jsonify(order_id=order_id, delivery_status="processing", message="주문 결과를 확인 중입니다."), 202
            result = {"order_id": order_id, "delivery_status": "unknown", "error": "이 주문의 전송 결과를 확인하지 못했습니다. #커피주문 채널을 확인해 주세요."}
            con.execute("UPDATE orders SET state='done', http_status=504, result=? WHERE id=?", (json.dumps(result, ensure_ascii=False), order_id))
            return jsonify(result), 504
        return jsonify(json.loads(existing["result"])), existing["http_status"]

    @app.before_request
    def gate():
        if not mobile_request():
            if request.path.startswith("/api/"):
                return jsonify(error="스마트폰에서 접속해 주세요."), 403
            return render_template("blocked.html"), 403
        if request.path.startswith("/api/") and request.method == "POST":
            # Same-origin JSON and bearer auth; no CORS permission is granted.
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                return jsonify(error="주문 페이지에서 다시 접속해 주세요."), 403
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                return jsonify(error="주문 페이지에서 다시 접속해 주세요."), 403

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Permissions-Policy"] = "accelerometer=(self), gyroscope=(self), magnetometer=(self)"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error="요청 형식이 올바르지 않습니다."), error.code
        return error

    @app.get("/")
    def index():
        sid = session_id() or secrets.token_urlsafe(24)
        response = make_response(render_template("index.html", menus=load_menus(), mock=app.config["SLACK_MOCK"]))
        # HTTPS tunneling is intentionally detected without trusting proxy headers.
        # HttpOnly + SameSite protects the cookie; Secure is set when the browser uses HTTPS.
        https_origin = request.is_secure or request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"
        response.set_cookie("coffee_session", signer.dumps(sid, salt="session"), httponly=True,
                            secure=https_origin, samesite="Strict", max_age=SESSION_TTL)
        return response

    @app.get("/api/menus")
    def menu_list():
        return jsonify(menus=[{"code": code, "name": menu["name"]} for code, menu in load_menus().items()])

    @app.post("/api/device/challenge")
    def challenge():
        sid = session_id()
        if not sid:
            return jsonify(error="페이지를 새로 열고 스마트폰 인증을 진행해 주세요."), 403
        now = time.time()
        nonce = secrets.token_urlsafe(24)
        with challenge_lock:
            for key in list(challenges):
                if challenges[key][1] <= now or challenges[key][0] == sid:
                    del challenges[key]
            if len(challenges) >= 1000:
                return jsonify(error="인증 요청이 많습니다. 잠시 후 다시 시도해 주세요."), 429
            challenges[nonce] = (sid, now + CHALLENGE_TTL)
        return jsonify(challenge=nonce, expires_in=CHALLENGE_TTL)

    @app.post("/api/device/verify")
    def verify():
        body = json_body()
        if not body or not sensor_proof_valid(body):
            return jsonify(error="스마트폰에서 접속해 주세요. 터치와 실제 센서 값을 확인하지 못했습니다."), 403
        nonce = body.get("challenge")
        if not isinstance(nonce, str):
            return jsonify(error="스마트폰 인증을 다시 진행해 주세요."), 403
        with challenge_lock:
            proof = challenges.pop(nonce, None)
        if not proof or proof[0] != session_id() or proof[1] <= time.time():
            return jsonify(error="스마트폰 인증이 만료되었습니다. 다시 인증해 주세요."), 403
        token = signer.dumps({"sid": proof[0], "ua": ua_hash()}, salt="mobile")
        return jsonify(token=token, expires_in=TOKEN_TTL)

    @app.post("/api/orders")
    def order():
        if not token_valid():
            return jsonify(error="스마트폰 인증이 필요합니다. 다시 인증해 주세요.", code="device_verification_required"), 403
        body = json_body()
        if not body:
            return jsonify(error="주문 정보를 확인해 주세요."), 400
        name = body.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 20:
            return jsonify(error="이름을 1~20자로 입력해 주세요."), 400
        name = name.strip()
        if "|" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
            return jsonify(error="이름에는 줄바꿈이나 | 기호를 사용할 수 없습니다."), 400
        code = body.get("menu_code")
        if not isinstance(code, str):
            return jsonify(error="목록에 있는 메뉴를 선택해 주세요."), 400
        raw_id = body.get("order_id")
        try:
            parsed_id = UUID(raw_id) if isinstance(raw_id, str) else None
            if parsed_id is None or parsed_id.version != 4 or str(parsed_id) != raw_id.lower():
                raise ValueError
        except (ValueError, AttributeError):
            return jsonify(error="유효한 UUID 주문 ID가 필요합니다."), 400
        order_id = str(parsed_id)
        fingerprint = hashlib.sha256(json.dumps([name, code], ensure_ascii=False).encode()).hexdigest()
        # Replays return the original result even if the announcement changed or is unavailable.
        with connect() as con:
            existing = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if existing:
                return previous_order(con, existing, order_id, fingerprint)
        menus = load_menus()
        if code not in menus:
            return jsonify(error="메뉴가 변경되었습니다. 페이지를 새로 열고 목록에 있는 메뉴를 선택해 주세요."), 400
        with connect() as con:
            # Durable claim commits before the Slack request. Concurrent requests cannot send twice.
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if existing:
                return previous_order(con, existing, order_id, fingerprint)
            if not app.config["SLACK_MOCK"] and not app.config["SLACK_WEBHOOK_URL"]:
                return jsonify(error="서버의 Slack Webhook이 설정되지 않았습니다. 관리자에게 알려 주세요."), 503
            con.execute("INSERT INTO orders (id, fingerprint, state, created) VALUES (?, ?, 'processing', ?)", (order_id, fingerprint, time.time()))

        menu_name = menus[code]["name"]  # Look up the source; never trust a submitted menu name.
        stamp = datetime.now(KST).strftime("%H:%M")
        display_text = f"{name} | {code} | {menu_name} | {stamp}"
        safe_text = f"{html.escape(name, quote=False)} | {code} | {html.escape(menu_name, quote=False)} | {stamp}"
        if app.config["SLACK_MOCK"]:
            with connect() as con:
                con.execute("INSERT INTO mock_messages (order_id, text) VALUES (?, ?)", (order_id, safe_text))
            print(f"[MOCK: Slack 미전송] {safe_text}", flush=True)
            status, outcome = 200, {"delivery_status": "mock"}
        else:
            status, outcome = send_slack(app.config["SLACK_WEBHOOK_URL"], safe_text, display_text)
        result = {"order_id": order_id, "name": name, "menu_code": code, "menu_name": menu_name,
                  "ordered_at": stamp, "mock": app.config["SLACK_MOCK"], **outcome}
        if status == 200:
            result["message"] = "연습 주문 완료" if app.config["SLACK_MOCK"] else "주문 완료"
        with connect() as con:
            con.execute("UPDATE orders SET state='done', http_status=?, result=? WHERE id=?", (status, json.dumps(result, ensure_ascii=False), order_id))
        return jsonify(result), status

    return app


def lan_ips():
    addresses = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))  # Select an interface; no packet is sent.
            addresses.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        addresses.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return list(dict.fromkeys(ip for ip in addresses if not ip.startswith("127."))) or ["127.0.0.1"]


if __name__ == "__main__":
    settings = dotenv_values(ROOT / ".env")
    port = int(os.getenv("PORT", settings.get("PORT") or "8000"))
    application = create_app()
    for ip in lan_ips():
        print(f"스마트폰 접속 주소: http://{ip}:{port}", flush=True)
    print("센서 인증에는 HTTPS가 필요합니다. ngrok http " + str(port), flush=True)
    print("모드: " + ("연습 (Slack 미전송)" if application.config["SLACK_MOCK"] else "실제 Slack 주문"), flush=True)
    if application.config["MENU_SOURCE"] == "slack":
        ready = bool(application.config["SLACK_BOT_TOKEN"] and application.config["SLACK_MENU_MESSAGE_URL"])
        print("메뉴 출처: Slack 공지" + ("" if ready else " — .env에 Bot 토큰과 메뉴 공지 링크를 설정해 주세요."), flush=True)
    else:
        print("메뉴 출처: 연습용 menus.json (실제 Slack 공지 조회 아님)", flush=True)
    application.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)
