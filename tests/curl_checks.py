"""실제 HTTP + curl 검증. 임시 DB와 mock 서버를 사용하므로 실제 Slack 미전송."""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from werkzeug.serving import make_server

UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"


def main():
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise SystemExit("curl을 설치한 뒤 다시 실행해 주세요.")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app({"DATABASE": root / "orders.sqlite3", "SLACK_MOCK": True, "SECRET_KEY": "curl-test-only", "SLACK_WEBHOOK_URL": "", "MENU_SOURCE": "file"})
        server = make_server("127.0.0.1", 0, app, threaded=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}"
        cookie = str(root / "cookies.txt")
        counter = 0

        def call(path, body=None, mobile=True, token=None):
            nonlocal counter
            counter += 1
            response_file = root / f"response-{counter}.json"
            args = [curl, "--silent", "--show-error", "--max-time", "15", "--noproxy", "*", "-o", str(response_file), "-w", "%{http_code}", "-b", cookie, "-c", cookie]
            args += ["-A", UA if mobile else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0"]
            if mobile:
                args += ["-H", "Sec-CH-UA-Mobile: ?1"]
            if token:
                args += ["-H", "Authorization: Bearer " + token]
            if body is not None:
                body_file = root / f"body-{counter}.json"
                body_file.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
                args += ["-H", "Content-Type: application/json", "--data-binary", "@" + str(body_file)]
            result = subprocess.run(args + [url + path], capture_output=True, text=True, check=True)
            try:
                data = json.loads(response_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = None
            return int(result.stdout), data

        try:
            assert call("/", mobile=False)[0] == 403
            assert call("/api/orders", {}, mobile=False)[0] == 403
            print("PASS (a) PC 페이지와 주문 API: 403")
            assert call("/")[0] == 200
            assert call("/api/orders", {})[0] == 403
            print("PASS (b) 모바일 UA + 토큰 없음: 403")
            nonce = call("/api/device/challenge", {})[1]["challenge"]
            # Synthetic evidence is ONLY an API test, never proof of a physical phone.
            evidence = {"challenge": nonce, "secure_context": True, "max_touch_points": 5, "coarse_pointer": True,
                        "sensor_kind": "motion", "samples": [{"t": 100, "values": [0, 0, 9.8]}, {"t": 300, "values": [0.1, 0.2, 9.7]}, {"t": 500, "values": [0.2, 0.3, 9.6]}]}
            status, verified = call("/api/device/verify", evidence)
            assert status == 200
            token = verified["token"]
            payload = {"name": "curl테스트", "menu_code": "M01", "order_id": str(uuid4())}
            assert call("/api/orders", {**payload, "name": " "}, token=token)[0] == 400
            print("PASS (c) 공백 이름: 400")
            assert call("/api/orders", {**payload, "menu_code": "UNKNOWN"}, token=token)[0] == 400
            print("PASS (d) 없는 메뉴 코드: 400")
            first, second = call("/api/orders", payload, token=token), call("/api/orders", payload, token=token)
            assert first[0] == second[0] == 200 and first[1] == second[1]
            with closing(sqlite3.connect(root / "orders.sqlite3")) as con:
                assert con.execute("SELECT count(*) FROM mock_messages").fetchone()[0] == 1
            print("PASS (e) 같은 UUID 2번: 동일 응답 / mock 전송 1번")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
