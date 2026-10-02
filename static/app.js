"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const form = $("order-form"), nameInput = $("customer-name"), orderButton = $("order-button");
  const radios = [...document.querySelectorAll('input[name="menu_code"]')];
  const panel = document.querySelector(".device-panel");
  const storageKey = "hanjan.pending.v1";
  let token = "", tokenUntil = 0, busy = false, verifying = false, pending = null, expiryTimer;

  function restorePending() {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey));
      if (value && typeof value.name === "string" && typeof value.menu_code === "string" && typeof value.order_id === "string" && radios.some(r => r.value === value.menu_code)) {
        pending = value;
        nameInput.value = value.name;
        radios.find(r => r.value === value.menu_code).checked = true;
      }
    } catch (_) { /* Private browsing may disable persistent storage. */ }
  }
  function savePending() {
    try { if (pending) localStorage.setItem(storageKey, JSON.stringify(pending)); else localStorage.removeItem(storageKey); } catch (_) {}
  }
  function currentMenu() { return radios.find(r => r.checked); }
  function validName() { return nameInput.value.trim().length > 0 && [...nameInput.value.trim()].length <= 20 && !/[|\x00-\x1f\x7f]/.test(nameInput.value); }
  function deviceReady() { return !!token && Date.now() < tokenUntil; }
  function update() {
    const menu = currentMenu();
    $("selected-menu").textContent = menu ? menu.closest("label").querySelector("strong").textContent : "한 잔을 골라 주세요";
    nameInput.disabled = busy || !!pending;
    radios.forEach(r => r.disabled = busy || !!pending);
    $("verify-button").disabled = busy || verifying;
    orderButton.disabled = busy || !deviceReady() || !validName() || !menu;
    $("order-button-label").textContent = busy ? "확인 중…" : pending ? "결과 확인하기" : "주문하기";
    $("order-hint").textContent = busy ? "주문 결과를 확인하고 있어요." : !deviceReady() ? "스마트폰 인증 후 주문할 수 있어요." : pending ? "이전 주문 ID로 결과만 확인합니다." : !validName() ? "이름을 입력해 주세요." : !menu ? "원하는 메뉴를 선택해 주세요." : "준비됐어요. 오늘의 한 잔을 주문해 주세요.";
  }
  function showError(message) { $("order-error").textContent = message; $("order-error").hidden = !message; }
  async function api(path, data, authenticated = false) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", ...(authenticated ? {Authorization: `Bearer ${token}`} : {})}, body: JSON.stringify(data), signal: controller.signal});
      const result = await response.json();
      return {response, result};
    } finally { clearTimeout(timer); }
  }
  function collectSensors() {
    return new Promise((resolve, reject) => {
      const groups = {motion: [], orientation: []};
      const started = performance.now();
      const cleanup = () => { clearTimeout(timer); window.removeEventListener("devicemotion", motion); window.removeEventListener("deviceorientation", orientation); };
      function record(kind, values, trusted) {
        if (!trusted || values.some(v => typeof v !== "number" || !Number.isFinite(v))) return;
        const t = Math.round(performance.now() - started);
        const samples = groups[kind];
        if (samples.length && t - samples[samples.length - 1].t < 100) return;
        samples.push({t, values});
        if (samples.length > 12) samples.shift();
        const changed = [0, 1, 2].some(axis => Math.max(...samples.map(s => s.values[axis])) - Math.min(...samples.map(s => s.values[axis])) >= 0.02);
        if (samples.length >= 3 && changed) { cleanup(); resolve({sensor_kind: kind, samples}); }
      }
      function motion(event) { const a = event.accelerationIncludingGravity; if (a) record("motion", [a.x, a.y, a.z], event.isTrusted); }
      function orientation(event) { record("orientation", [event.alpha, event.beta, event.gamma], event.isTrusted); }
      const timer = setTimeout(() => { cleanup(); reject(new Error("스마트폰에서 접속해 주세요. 센서 값을 확인하지 못했습니다. Safari 또는 Chrome에서 센서 권한을 허용하고 휴대폰을 살짝 기울여 주세요.")); }, 10000);
      window.addEventListener("devicemotion", motion);
      window.addEventListener("deviceorientation", orientation);
    });
  }
  $("verify-button").addEventListener("click", async () => {
    if (verifying || busy) return;
    verifying = true;
    token = "";
    panel.classList.remove("verified", "error");
    $("device-status").textContent = "스마트폰을 확인하고 있어요…";
    update();
    try {
      if (!window.isSecureContext) throw new Error("센서 인증에는 HTTPS 주소가 필요해요. 노트북에서 ngrok http 8000을 실행한 뒤 표시된 https:// 주소를 휴대폰에서 열어 주세요.");
      if (!(navigator.maxTouchPoints > 0 && matchMedia("(pointer: coarse)").matches)) throw new Error("스마트폰에서 접속해 주세요. 터치 가능한 실제 스마트폰이 필요합니다.");
      // iOS permission requests MUST start synchronously inside this tap handler.
      const requests = [window.DeviceMotionEvent, window.DeviceOrientationEvent].filter(C => C && typeof C.requestPermission === "function").map(C => C.requestPermission());
      if (requests.length) {
        const permissions = await Promise.allSettled(requests);
        if (!permissions.some(p => p.status === "fulfilled" && p.value === "granted")) throw new Error("움직임 및 방향 권한을 허용해 주세요. 브라우저 설정에서 권한을 바꾼 후 다시 인증해 주세요.");
      }
      const challenge = await api("/api/device/challenge", {});
      if (!challenge.response.ok) throw new Error(challenge.result.error || "인증 요청에 실패했습니다.");
      $("device-status").textContent = "휴대폰을 살짝 기울여 주세요. 센서 확인 중…";
      const proof = await collectSensors();
      const verified = await api("/api/device/verify", {...proof, challenge: challenge.result.challenge, max_touch_points: navigator.maxTouchPoints, coarse_pointer: matchMedia("(pointer: coarse)").matches, secure_context: window.isSecureContext});
      if (!verified.response.ok) throw new Error(verified.result.error || "스마트폰 인증에 실패했습니다.");
      token = verified.result.token;
      tokenUntil = Date.now() + verified.result.expires_in * 1000;
      panel.classList.add("verified");
      $("device-title").textContent = "스마트폰 인증 완료";
      $("device-description").textContent = "이제 이름과 메뉴를 선택해 주문할 수 있어요.";
      $("device-status").textContent = "인증은 5분 동안 유효해요.";
      panel.querySelector(".step-icon").textContent = "✓";
      clearTimeout(expiryTimer);
      expiryTimer = setTimeout(() => {
        token = ""; panel.classList.remove("verified"); $("device-title").textContent = "스마트폰을 다시 확인할게요";
        $("device-status").textContent = "5분이 지나 인증이 만료됐어요. 다시 인증해 주세요."; update();
      }, verified.result.expires_in * 1000);
    } catch (error) {
      panel.classList.add("error");
      $("device-status").textContent = error.message || "인증에 실패했습니다. 다시 시도해 주세요.";
    } finally { verifying = false; update(); }
  });
  nameInput.addEventListener("input", () => { $("name-error").textContent = ""; nameInput.removeAttribute("aria-invalid"); update(); });
  nameInput.addEventListener("blur", () => { if (nameInput.value && !validName()) { $("name-error").textContent = "이름을 1~20자로 입력해 주세요. | 기호와 줄바꿈은 사용할 수 없어요."; nameInput.setAttribute("aria-invalid", "true"); } });
  radios.forEach(r => r.addEventListener("change", update));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy) return;
    if (!deviceReady()) { showError("스마트폰 인증을 먼저 진행해 주세요."); return; }
    if (!validName() || !currentMenu()) { showError("이름과 메뉴를 확인해 주세요."); return; }
    busy = true; showError(""); update();
    if (!pending) { pending = {name: nameInput.value.trim(), menu_code: currentMenu().value, order_id: crypto.randomUUID()}; savePending(); }
    update();
    try {
      let outcome;
      for (let attempt = 0; attempt < 6; attempt++) {
        outcome = await api("/api/orders", pending, true);
        if (outcome.response.status !== 202) break;
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
      const {response, result} = outcome;
      if (response.status === 202) throw new Error("주문 처리 중입니다. 잠시 후 결과 확인하기를 눌러 주세요.");
      if (!response.ok) {
        if (result.code === "device_verification_required") { token = ""; panel.classList.remove("verified"); $("device-status").textContent = "인증이 만료됐어요. 다시 인증 후 이전 주문 결과를 확인해 주세요."; }
        // Confirmed non-delivery can start a new order; uncertain delivery must retain its ID.
        if (result.delivery_status === "failed" || [400, 503].includes(response.status)) { pending = null; savePending(); }
        throw new Error(result.error || "주문 결과를 확인하지 못했습니다.");
      }
      if (!["sent", "mock"].includes(result.delivery_status)) throw new Error("주문 결과를 확인하지 못했습니다.");
      $("receipt-title").textContent = result.mock ? "연습 주문 완료" : "주문 완료";
      $("receipt-subtitle").textContent = result.mock ? "연습 모드라 Slack에는 전송되지 않았어요." : "Slack #커피주문 채널에 주문을 보냈어요.";
      $("receipt-name").textContent = result.name;
      $("receipt-code").textContent = result.menu_code;
      $("receipt-menu").textContent = result.menu_name;
      $("receipt-time").textContent = `${result.ordered_at} · 한국 시간`;
      $("receipt-state").textContent = result.mock ? "연습 주문 완료 (미전송)" : "주문 완료";
      form.hidden = true; panel.hidden = true; $("receipt").hidden = false; document.body.classList.add("complete");
      pending = null; savePending();
      $("receipt").scrollIntoView({behavior: "smooth", block: "start"});
    } catch (error) { showError(error.name === "AbortError" || error instanceof TypeError ? "연결이 끊겨 주문 결과를 확인하지 못했어요. 결과 확인하기를 누르면 같은 주문 ID로 확인하므로 중복 전송되지 않습니다." : error.message); }
    finally { busy = false; update(); }
  });
  $("new-order").addEventListener("click", () => { form.hidden = false; panel.hidden = false; $("receipt").hidden = true; document.body.classList.remove("complete"); radios.forEach(r => r.checked = false); showError(""); update(); window.scrollTo({top: 0, behavior: "smooth"}); });
  restorePending();
  if (pending) showError("확인하지 못한 이전 주문이 있어요. 스마트폰 인증 후 결과 확인하기를 눌러 주세요.");
  update();
})();
