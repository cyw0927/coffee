#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
MOBILE_UA='Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36'
PC_UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36'
H=(-H "User-Agent: $MOBILE_UA" -H 'Sec-CH-UA-Mobile: ?1' -H 'Content-Type: application/json')

echo '[a] PC UA -> 403'
curl -s -o /dev/null -w '%{http_code}\n' -H "User-Agent: $PC_UA" -H 'Sec-CH-UA-Mobile: ?0' "$BASE/"

echo '[b] 모바일 UA지만 토큰 없음 -> 403'
curl -s -o /dev/null -w '%{http_code}\n' "${H[@]}" -X POST "$BASE/api/order" \
  -d '{"order_id":"11111111-1111-4111-8111-111111111111","name":"테스트","menu_code":"M01","device_token":"not-a-valid-device-token-xxxxxxxx"}'

# curl 검증은 실제 센서 자체를 검증하는 테스트가 아니라 서버 검증 경로를 확인하기 위한 합성 proof다.
CHALLENGE=$(curl -s "${H[@]}" -X POST "$BASE/api/device/challenge" -d '{}' | python -c 'import sys,json; print(json.load(sys.stdin)["challenge_id"])')
TOKEN=$(curl -s "${H[@]}" -X POST "$BASE/api/device/verify" -d "{\"challenge_id\":\"$CHALLENGE\",\"max_touch_points\":5,\"coarse_pointer\":true,\"motion\":{\"x\":0.1,\"y\":0.2,\"z\":9.7}}" | python -c 'import sys,json; print(json.load(sys.stdin)["device_token"])')

echo '[c] 이름 빈값 -> 400'
curl -s -o /dev/null -w '%{http_code}\n' "${H[@]}" -X POST "$BASE/api/order" \
  -d "{\"order_id\":\"22222222-2222-4222-8222-222222222222\",\"name\":\"   \",\"menu_code\":\"M01\",\"device_token\":\"$TOKEN\"}"

echo '[d] 없는 메뉴 -> 400'
curl -s -o /dev/null -w '%{http_code}\n' "${H[@]}" -X POST "$BASE/api/order" \
  -d "{\"order_id\":\"33333333-3333-4333-8333-333333333333\",\"name\":\"테스트\",\"menu_code\":\"M99\",\"device_token\":\"$TOKEN\"}"

echo '[e] 같은 주문 ID 두 번 -> 둘 다 성공 응답, mock Slack count는 1 증가'
OID='44444444-4444-4444-8444-444444444444'
curl -s "${H[@]}" -X POST "$BASE/api/order" -d "{\"order_id\":\"$OID\",\"name\":\"테스트\",\"menu_code\":\"M01\",\"device_token\":\"$TOKEN\"}"; echo
curl -s "${H[@]}" -X POST "$BASE/api/order" -d "{\"order_id\":\"$OID\",\"name\":\"테스트\",\"menu_code\":\"M01\",\"device_token\":\"$TOKEN\"}"; echo
curl -s "${H[@]}" "$BASE/api/debug/mock-messages"; echo
