import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from menu_source import MenuSourceError, SlackMenuSource, menu_json_from_text, message_location
from test_app import HEADERS, proof

LINK = "https://example.slack.com/archives/C123ABC/p1790920000123456"
STAMP = "1790920000.123456"
CATALOG = [{"code": "LIVE42", "name": "공지에서 읽은 메뉴"}]


def response_for(opener, data):
    response = opener.return_value.open.return_value.__enter__.return_value
    response.status = 200
    response.read.return_value = json.dumps(data, ensure_ascii=False).encode("utf-8")


def message(menus=CATALOG):
    return {"ok": True, "messages": [{"ts": STAMP, "text": "오늘의 메뉴\n```json\n" + json.dumps(menus, ensure_ascii=False) + "\n```"}]}


class SlackMenusTest(unittest.TestCase):
    def test_message_url_and_json_extraction(self):
        self.assertEqual(message_location(LINK + "?thread_ts=1790920000.123456"), ("C123ABC", STAMP))
        for link in ("http://example.slack.com/archives/C123ABC/p1790920000123456", "https://evil.example/archives/C123ABC/p1790920000123456", "not-a-link"):
            with self.assertRaises(MenuSourceError):
                message_location(link)
        text = '[공지]\n```json\n[\n\u00a0 {"code":"Z99", "name":"A &amp; B"}\n]\n```'
        self.assertEqual(menu_json_from_text(text)["Z99"]["name"], "A & B")

    def test_invalid_ambiguous_duplicate_and_truncated_arrays(self):
        for text in (None, "", "[]", '[{"code":"M01","name":"메뉴"}',
                     '[{"code":"M01","name":"메뉴"},{"code":"M01","name":"다른 메뉴"}]',
                     '[{"code":"M01","name":""}]', '[{"code":"M01","name":"메뉴|분리"}]',
                     '[{"code":"M01","name":"메뉴"}] [{"code":"M02","name":"두 번째"}]'):
            with self.subTest(text=text), self.assertRaises(MenuSourceError):
                menu_json_from_text(text)

    def test_api_auth_parameters_cache_and_refresh(self):
        source = SlackMenuSource("test-token", LINK)
        with patch("menu_source.build_opener") as opener, patch("menu_source.time.monotonic", return_value=100):
            response_for(opener, message())
            self.assertEqual(source.read()["LIVE42"]["name"], "공지에서 읽은 메뉴")
            self.assertEqual(source.read()["LIVE42"]["name"], "공지에서 읽은 메뉴")
            opener.return_value.open.assert_called_once()
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.get_header("Authorization"), "Bearer test-token")
            self.assertNotIn("test-token", req.full_url)
            self.assertEqual(parse_qs(urlsplit(req.full_url).query), {"channel": ["C123ABC"], "latest": [STAMP], "inclusive": ["true"], "limit": ["1"]})
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 5)
        with patch("menu_source.build_opener") as opener, patch("menu_source.time.monotonic", return_value=161):
            response_for(opener, message([{"code": "NEW77", "name": "수정된 공지 메뉴"}]))
            self.assertEqual(list(source.read()), ["NEW77"])

    def test_source_errors_and_no_old_catalog_fallback(self):
        for data in ({"ok": False, "error": "missing_scope"}, {"ok": True, "messages": []},
                     {"ok": True, "messages": [{"ts": "wrong", "text": json.dumps(CATALOG)}]},
                     {"ok": True, "messages": [{"ts": STAMP, "text": "잘못된 메뉴"}]}):
            with patch("menu_source.build_opener") as opener:
                response_for(opener, data)
                with self.assertRaises(MenuSourceError):
                    SlackMenuSource("test-token", LINK).read()
        for error in (TimeoutError(), URLError("network"), HTTPError("https://slack.com", 429, "rate_limited", {}, None)):
            with patch("menu_source.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(MenuSourceError):
                    SlackMenuSource("test-token", LINK).read()
        source = SlackMenuSource("test-token", LINK)
        with patch("menu_source.build_opener") as opener, patch("menu_source.time.monotonic", return_value=100):
            response_for(opener, message())
            source.read()
        with patch("menu_source.build_opener") as opener, patch("menu_source.time.monotonic", return_value=161):
            opener.return_value.open.side_effect = TimeoutError()
            with self.assertRaises(MenuSourceError):
                source.read()
            with self.assertRaises(MenuSourceError):
                source.read()
            opener.return_value.open.assert_called_once()

    def test_page_order_and_replay_use_slack_source(self):
        with tempfile.TemporaryDirectory() as directory, patch("menu_source.build_opener") as opener, patch("menu_source.CACHE_SECONDS", 0):
            response_for(opener, message())
            app = create_app({"TESTING": True, "SLACK_MOCK": True, "DATABASE": Path(directory) / "orders.db",
                              "MENU_SOURCE": "slack", "SLACK_BOT_TOKEN": "test-token", "SLACK_MENU_MESSAGE_URL": LINK})
            client = app.test_client()
            page = client.get("/", headers=HEADERS)
            self.assertEqual(page.status_code, 200)
            self.assertIn("공지에서 읽은 메뉴", page.get_data(as_text=True))
            self.assertNotIn("test-token", page.get_data(as_text=True))
            nonce = client.post("/api/device/challenge", headers=HEADERS, json={}).json["challenge"]
            token = client.post("/api/device/verify", headers=HEADERS, json=proof(nonce)).json["token"]
            auth = {**HEADERS, "Authorization": "Bearer " + token}
            payload = {"name": "실시간검증", "menu_code": "LIVE42", "menu_name": "위조", "order_id": str(uuid4())}
            first = client.post("/api/orders", headers=auth, json=payload)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json["menu_name"], "공지에서 읽은 메뉴")
            response_for(opener, message([{"code": "NEW77", "name": "새 메뉴"}]))
            self.assertEqual(client.post("/api/orders", headers=auth, json={**payload, "order_id": str(uuid4())}).status_code, 400)
            opener.return_value.open.side_effect = TimeoutError()
            self.assertEqual(client.post("/api/orders", headers=auth, json=payload).json, first.json)
            self.assertEqual(client.post("/api/orders", headers=auth, json={**payload, "order_id": str(uuid4())}).status_code, 503)
            self.assertEqual(client.get("/", headers=HEADERS).status_code, 503)
            with closing(sqlite3.connect(Path(directory) / "orders.db")) as con:
                self.assertEqual(con.execute("SELECT count(*) FROM mock_messages").fetchone()[0], 1)

    def test_file_is_mock_only_and_reloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "menus.json"
            file.write_text(json.dumps(CATALOG), encoding="utf-8")
            app = create_app({"SLACK_MOCK": True, "MENU_SOURCE": "file", "MENU_FILE": file, "DATABASE": Path(directory) / "orders.db"})
            client = app.test_client()
            self.assertEqual(client.get("/api/menus", headers=HEADERS).json["menus"], CATALOG)
            file.write_text('[{"code":"UPDATED", "name":"변경된 메뉴"}]', encoding="utf-8")
            self.assertEqual(client.get("/api/menus", headers=HEADERS).json["menus"][0]["code"], "UPDATED")
            file.unlink()
            self.assertEqual(client.get("/api/menus", headers=HEADERS).status_code, 503)
            with self.assertRaises(ValueError):
                create_app({"SLACK_MOCK": False, "MENU_SOURCE": "file", "DATABASE": Path(directory) / "orders.db"})


if __name__ == "__main__":
    unittest.main()
