"""표준 라이브러리만 사용하는 자동 검증. 실제 Slack으로 전송하지 않습니다."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app, send_slack, sensor_proof_valid

UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"
HEADERS = {"User-Agent": UA, "Sec-CH-UA-Mobile": "?1"}


def proof(challenge):
    return {"challenge": challenge, "secure_context": True, "max_touch_points": 5, "coarse_pointer": True,
            "sensor_kind": "motion", "samples": [{"t": 100, "values": [0, 0, 9.8]}, {"t": 250, "values": [0.1, 0.2, 9.7]}, {"t": 400, "values": [0.3, 0.4, 9.6]}]}


class OrdersTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "orders.sqlite3"
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-only-secret", "DATABASE": self.db, "SLACK_MOCK": True, "SLACK_WEBHOOK_URL": "", "MENU_SOURCE": "file"})
        self.client = self.app.test_client()
        self.client.get("/", headers=HEADERS)
        self.token = self.authenticate()
        self.auth = {**HEADERS, "Authorization": "Bearer " + self.token}

    def tearDown(self):
        self.directory.cleanup()

    def authenticate(self):
        challenge = self.client.post("/api/device/challenge", headers=HEADERS, json={}).json["challenge"]
        response = self.client.post("/api/device/verify", headers=HEADERS, json=proof(challenge))
        self.assertEqual(response.status_code, 200)
        return response.json["token"]

    def order(self, **changes):
        payload = {"name": "홍길동", "menu_code": "M01", "order_id": str(uuid4()), **changes}
        return self.client.post("/api/orders", headers=self.auth, json=payload)

    def count(self, table):
        with closing(sqlite3.connect(self.db)) as con:
            return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_pc_page_and_api_blocked(self):
        for path in ("/", "/static/app.js"):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.post("/api/orders", json={}).status_code, 403)
        self.assertEqual(self.client.get("/", headers={**HEADERS, "Sec-CH-UA-Mobile": "?0"}).status_code, 403)

    def test_mobile_requires_token(self):
        self.assertEqual(self.client.post("/api/orders", headers=HEADERS, json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/orders", headers={**HEADERS, "Authorization": "Bearer forged"}, json={}).status_code, 403)

    def test_token_is_bound_to_cookie_and_ua(self):
        other = self.app.test_client()
        self.assertEqual(other.post("/api/orders", headers=self.auth, json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/orders", headers={**self.auth, "User-Agent": UA + " changed"}, json={}).status_code, 403)

    def test_token_expiry(self):
        with patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=int(time.time()) + 301):
            self.assertEqual(self.order().status_code, 403)

    def test_invalid_names(self):
        for name in ("", "  ", None, 12, "가" * 21, "홍\n길동", "홍|길동"):
            with self.subTest(name=name):
                self.assertEqual(self.order(name=name).status_code, 400)
        self.assertEqual(self.count("mock_messages"), 0)

    def test_bad_menu_and_id(self):
        for code in ("A99", ["M01"], None):
            self.assertEqual(self.order(menu_code=code).status_code, 400)
        self.assertEqual(self.order(order_id="not-a-uuid").status_code, 400)

    def test_authoritative_menu_kst_and_escaping(self):
        response = self.order(name="  <!here>&홍길동  ", menu_code="M02", menu_name="위조 메뉴명")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["menu_name"], "아이스라떼")
        self.assertEqual(response.json["name"], "<!here>&홍길동")
        self.assertRegex(response.json["ordered_at"], r"^\d{2}:\d{2}$")
        with closing(sqlite3.connect(self.db)) as con:
            text = con.execute("SELECT text FROM mock_messages").fetchone()[0]
        self.assertTrue(text.startswith("&lt;!here&gt;&amp;홍길동 | M02 | 아이스라떼 | "))

    def test_same_id_returns_identical_result_once_even_after_restart(self):
        order_id = str(uuid4())
        first, second = self.order(order_id=order_id), self.order(order_id=order_id)
        self.assertEqual(first.json, second.json)
        self.assertEqual(self.count("mock_messages"), 1)
        self.assertEqual(self.order(order_id=order_id, menu_code="M02").status_code, 409)
        self.app = create_app(dict(self.app.config))
        self.client = self.app.test_client()
        self.client.get("/", headers=HEADERS)
        self.auth = {**HEADERS, "Authorization": "Bearer " + self.authenticate()}
        self.assertEqual(self.order(order_id=order_id).json, first.json)
        self.assertEqual(self.count("mock_messages"), 1)

    def test_concurrent_retries_send_once(self):
        order_id = str(uuid4())
        cookie = self.client.get_cookie("coffee_session").value
        def post(_):
            with self.app.test_client() as client:
                client.set_cookie("coffee_session", cookie)
                return client.post("/api/orders", headers=self.auth, json={"name": "동시주문", "menu_code": "M03", "order_id": order_id}).status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(post, range(12)))
        self.assertTrue(all(s in (200, 202) for s in statuses), statuses)
        self.assertEqual(self.count("mock_messages"), 1)

    def test_slack_failure_cached_and_never_success(self):
        self.app.config.update(SLACK_MOCK=False, SLACK_WEBHOOK_URL="https://hooks.slack.com/services/test/test/test")
        for status, state in ((502, "failed"), (504, "unknown")):
            order_id = str(uuid4())
            with patch("app.send_slack", return_value=(status, {"delivery_status": state, "error": "테스트 실패"})) as sender:
                first, second = self.order(order_id=order_id), self.order(order_id=order_id)
                self.assertEqual(first.status_code, status)
                self.assertEqual(first.json, second.json)
                self.assertNotIn("message", first.json)
                sender.assert_called_once()

    def test_missing_webhook_and_cross_origin(self):
        self.app.config.update(SLACK_MOCK=False, SLACK_WEBHOOK_URL="")
        self.assertEqual(self.order().status_code, 503)
        self.assertEqual(self.client.post("/api/orders", headers={**self.auth, "Origin": "https://evil.example"}, json={}).status_code, 403)

    def test_sensor_null_static_and_challenge_replay(self):
        for changes in ({"max_touch_points": 0}, {"coarse_pointer": False}, {"secure_context": False}, {"samples": [{"t": 100, "values": [None, None, None]}] * 3}, {"samples": [{"t": n, "values": [0, 0, 0]} for n in (100, 300, 500)]}):
            self.assertFalse(sensor_proof_valid({**proof("x"), **changes}))
        nonce = self.client.post("/api/device/challenge", headers=HEADERS, json={}).json["challenge"]
        self.assertEqual(self.client.post("/api/device/verify", headers=HEADERS, json=proof(nonce)).status_code, 200)
        self.assertEqual(self.client.post("/api/device/verify", headers=HEADERS, json=proof(nonce)).status_code, 403)

    def test_stale_processing_is_not_resent(self):
        order_id = str(uuid4())
        with patch("app.send_slack", side_effect=RuntimeError("simulated crash")):
            self.app.config.update(SLACK_MOCK=False, SLACK_WEBHOOK_URL="https://hooks.slack.com/services/test/test/test")
            with self.assertRaises(RuntimeError):
                self.order(order_id=order_id)
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("UPDATE orders SET created=?", (time.time() - 31,))
            con.commit()
        with patch("app.send_slack") as sender:
            self.assertEqual(self.order(order_id=order_id).json["delivery_status"], "unknown")
            sender.assert_not_called()


class SlackTransportTest(unittest.TestCase):
    def test_timeout_http_error_non200_and_success(self):
        for failure, status in ((TimeoutError(), 504), (URLError("timeout"), 504), (HTTPError("https://example", 403, "forbidden", {}, None), 502)):
            with patch("app.build_opener") as opener:
                opener.return_value.open.side_effect = failure
                actual, _ = send_slack("https://example", "escaped", "original")
                self.assertEqual(actual, status)
                self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 5)
        for http_status, body, status in ((200, b"ok", 200), (201, b"ok", 502), (200, b"error", 502)):
            with patch("app.build_opener") as opener:
                response = opener.return_value.open.return_value.__enter__.return_value
                response.status, response.read.return_value = http_status, body
                self.assertEqual(send_slack("https://example", "&lt;!here&gt;", "<!here>")[0], status)
                payload = json.loads(opener.return_value.open.call_args.args[0].data)
                self.assertFalse(payload["mrkdwn"])
                self.assertEqual(payload["blocks"][0]["text"]["type"], "plain_text")


if __name__ == "__main__":
    unittest.main()
