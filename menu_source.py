"""Slack 공지 메시지에서 메뉴 JSON을 읽습니다. menus.json은 mock 전용입니다."""
import html
import json
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_MENU_BYTES = 65536
MENU_TIMEOUT = 5
CACHE_SECONDS = 60


class MenuSourceError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_menus(data) -> dict:
    if not isinstance(data, list) or not 1 <= len(data) <= 100:
        raise MenuSourceError("메뉴 공지에 올바른 JSON 배열이 필요합니다.")
    menus = {}
    for item in data:
        if not isinstance(item, dict):
            raise MenuSourceError("메뉴 항목에 code와 name이 필요합니다.")
        code, name = item.get("code"), item.get("name")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", code) or code in menus:
            raise MenuSourceError("메뉴 코드가 올바르지 않거나 중복되어 있습니다.")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or "|" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise MenuSourceError("메뉴명이 올바르지 않습니다.")
        # These are presentation defaults; codes and names only come from the source.
        style = "matcha" if "말차" in name else "latte" if "라떼" in name or "라테" in name else "tea iced" if name.endswith("티") else "americano"
        if "아이스" in name and style in ("americano", "latte"):
            style += " iced"
        menus[code] = {"name": name.strip(), "description": "마음에 드는 한 잔을 선택해 주세요", "style": style}
    return menus


def menu_json_from_text(text: str) -> dict:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MENU_BYTES:
        raise MenuSourceError("메뉴 공지 형식이 올바르지 않습니다.")
    # Slack escapes &, < and >. Normalize non-breaking indentation outside JSON strings.
    text = html.unescape(text)
    normalized, quoted, escaped = [], False, False
    for char in text:
        normalized.append(" " if char == "\u00a0" and not quoted else char)
        if char == '"' and not escaped:
            quoted = not quoted
        escaped = char == "\\" and not escaped
    text = "".join(normalized)
    decoder = json.JSONDecoder()
    candidates = []
    for match in re.finditer(r"\[", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
            candidates.append(validate_menus(value))
        except (ValueError, RecursionError, MenuSourceError):
            continue
    if len(candidates) != 1:
        raise MenuSourceError("메뉴 공지에 code/name으로 된 JSON 배열이 하나 있어야 합니다. 닫는 ] 기호까지 확인해 주세요.")
    return candidates[0]


def message_location(message_url: str) -> tuple[str, str]:
    parsed = urlsplit(message_url.strip())
    match = re.fullmatch(r"/archives/([CG][A-Z0-9]+)/p(\d{10})(\d{6})/?", parsed.path)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".slack.com") or not match or parsed.username or parsed.password:
        raise MenuSourceError(".env에 Slack 메뉴 공지 메시지의 링크를 설정해 주세요.")
    return match[1], match[2] + "." + match[3]


class SlackMenuSource:
    def __init__(self, token: str, message_url: str):
        self.token = token
        self.message_url = message_url
        self._lock = threading.Lock()
        self._menus = None
        self._error = None
        self._until = 0

    def _fetch(self) -> dict:
        if not self.token:
            raise MenuSourceError("Slack 메뉴 읽기 설정이 필요합니다. 관리자에게 알려 주세요.")
        channel, stamp = message_location(self.message_url)
        query = urlencode({"channel": channel, "latest": stamp, "inclusive": "true", "limit": 1})
        req = Request("https://slack.com/api/conversations.history?" + query,
                      headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"})
        try:
            with build_opener(NoRedirect()).open(req, timeout=MENU_TIMEOUT) as response:
                if response.status != 200:
                    raise MenuSourceError("Slack 메뉴를 읽지 못했습니다. 잠시 후 다시 시도해 주세요.")
                raw = response.read(MAX_MENU_BYTES + 1)
            if len(raw) > MAX_MENU_BYTES:
                raise ValueError
            result = json.loads(raw.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError
            if result.get("ok") is not True:
                reasons = {"invalid_auth": "메뉴 읽기 토큰을 확인해 주세요.", "not_authed": "메뉴 읽기 토큰을 확인해 주세요.",
                           "missing_scope": "봇에 채널 기록 읽기 권한이 필요합니다.", "not_in_channel": "메뉴 공지 채널에 봇을 초대해 주세요.",
                           "channel_not_found": "메뉴 공지 링크와 봇의 채널 접근 권한을 확인해 주세요."}
                error_code = result.get("error")
                reason = reasons.get(error_code if isinstance(error_code, str) else "", "잠시 후 다시 시도해 주세요.")
                raise MenuSourceError("Slack 메뉴를 읽지 못했습니다. " + reason)
            messages = result.get("messages")
            if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict) or messages[0].get("ts") != stamp:
                raise MenuSourceError("지정한 메뉴 공지를 찾지 못했습니다. 채널 본문에 있는 공지 메시지의 링크를 확인해 주세요.")
            return menu_json_from_text(messages[0].get("text"))
        except HTTPError as error:
            if error.code == 429:
                raise MenuSourceError("Slack 메뉴 조회 제한에 도달했습니다. 잠시 후 다시 시도해 주세요.") from None
            raise MenuSourceError("Slack 메뉴를 읽지 못했습니다. 연결과 읽기 권한을 확인해 주세요.") from None
        except (OSError, URLError, ValueError, UnicodeError, RecursionError):
            raise MenuSourceError("Slack 메뉴를 읽지 못했습니다. 연결과 공지 형식을 확인해 주세요.") from None

    def read(self) -> dict:
        # A bounded cache avoids repeatedly reading the same announcement on every tap.
        with self._lock:
            if time.monotonic() < self._until:
                if self._error:
                    raise MenuSourceError(self._error)
                return self._menus
            try:
                self._menus = self._fetch()
                self._error = None
            except MenuSourceError as error:
                self._error = str(error)
                raise
            finally:
                self._until = time.monotonic() + CACHE_SECONDS
            return self._menus


def read_mock_menus(menu_file: Path) -> dict:
    try:
        with Path(menu_file).open("rb") as source:
            raw = source.read(MAX_MENU_BYTES + 1)
        if len(raw) > MAX_MENU_BYTES:
            raise ValueError
        return validate_menus(json.loads(raw.decode("utf-8-sig")))
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise MenuSourceError("연습용 메뉴 JSON 파일을 읽지 못했습니다.") from None
