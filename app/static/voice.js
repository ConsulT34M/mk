"use strict";

(() => {
  const dialog = document.getElementById("voice-dialog");
  const start = document.getElementById("voice-start");
  const stop = document.getElementById("voice-stop");
  const state = document.getElementById("voice-state");
  const receipt = document.getElementById("voice-ticket");
  const audio = document.getElementById("voice-audio");
  const consent = document.getElementById("voice-consent");
  const recordingState = document.getElementById("voice-recording-state");
  const sessions = dialog.dataset.sessionUrl;
  let active = null;
  let finishing = false;

  function releaseSession(id) {
    // The server also removes unused sessions when the customer's cookie expires.
    return api(sessions + "/" + id + "/end", {method: "POST", body: {}, keepalive: true});
  }

  async function finish(message) {
    const call = active;
    if (!call) return;
    active = null;
    finishing = true;
    clearTimeout(call.timer);
    call.controller.abort();
    const drained = call.recorder?.stop();
    call.stream?.getTracks().forEach(track => track.stop());
    call.channel?.close();
    call.peer?.close();
    audio.pause();
    audio.srcObject = null;
    audio.hidden = true;
    stop.hidden = true;
    state.textContent = message;
    const wasRecording = Boolean(call.recorder?.url);
    if (wasRecording) recordingState.textContent = "녹취를 저장하고 있어요.";
    try {
      await drained;
      const result = call.id && await releaseSession(call.id);
      if (wasRecording) {
        recordingState.textContent = result?.recorded
          ? (call.recorder.failed ? "서버에 도착한 부분까지 녹취를 저장했어요." : "통화 녹취를 저장했어요.")
          : "저장된 녹취가 없어요.";
      }
    } catch {
      if (wasRecording) recordingState.textContent = "녹취 저장 결과를 확인할 수 없어요.";
    }
    finishing = false;
    start.hidden = false;
    consent.checked = false;
    consent.disabled = false;
    if (dialog.open) start.focus();
  }

  function send(call, event) {
    if (active === call && call.channel.readyState === "open") call.channel.send(JSON.stringify(event));
  }

  async function handleResponse(call, response) {
    if (response.status === "failed") {
      finish("AI가 음성으로 답하지 못했어요. 잠시 후 다시 시작해 주세요.");
      return;
    }
    if (response.status !== "completed") return;
    const requests = (response.output || []).filter(item => item.type === "function_call" &&
      item.name === "request_agent" && item.call_id && !call.handled.has(item.call_id));
    if (!requests.length) return;
    requests.forEach(item => call.handled.add(item.call_id));
    let output;
    try {
      const ticket = await api(sessions + "/" + call.id + "/handoff", {
        method: "POST", body: {}, signal: call.controller.signal,
      });
      if (active !== call) return;
      const number = ticket.case_id.slice(0, 8).toUpperCase();
      receipt.textContent = "상담원 요청을 접수했어요. 티켓 #" + number;
      receipt.hidden = false;
      output = {accepted: true, ticket_number: number};
    } catch {
      if (active !== call) return;
      state.textContent = "접수 결과를 확인할 수 없어요. 상담원 연결을 다시 요청해 주세요.";
      output = {accepted: false, message: "접수 결과 미확인. 고객에게 재시도를 안내하세요."};
    }
    for (const item of requests) {
      send(call, {type: "conversation.item.create", item: {
        type: "function_call_output", call_id: item.call_id, output: JSON.stringify(output),
      }});
    }
    send(call, {type: "response.create", response: {tool_choice: "none"}});
  }

  function connectMedia(call) {
    const peer = new RTCPeerConnection();
    call.peer = peer;
    peer.onconnectionstatechange = () => {
      if (active === call && peer.connectionState === "failed") finish("음성 연결이 끊겼어요. 다시 시작해 주세요.");
    };
    peer.ontrack = event => {
      if (active !== call) return;
      audio.srcObject = event.streams[0] || new MediaStream([event.track]);
      if (call.recorder?.url) call.recorder.attach(audio.srcObject);
      audio.play().catch(() => { if (active === call) audio.hidden = false; });
    };
    peer.addTrack(call.stream.getAudioTracks()[0], call.stream);
    const channel = peer.createDataChannel("oai-events");
    call.channel = channel;
    channel.onopen = () => {
      if (active !== call) return;
      clearTimeout(call.timer);
      if (call.recorder) {
        try {
          call.recorder.start(sessions + "/" + call.id + "/recording", call.stream, audio.srcObject);
          recordingState.hidden = false;
          recordingState.textContent = "녹취 중 · 고객과 AI의 음성을 저장하고 있어요.";
        } catch {
          finish("녹취를 시작할 수 없어요. 다시 시도해 주세요.");
          return;
        }
      }
      state.textContent = "AI와 연결됐어요. 편하게 말씀해 주세요.";
      send(call, {type: "response.create", response: {
        tool_choice: "none",
        instructions: "한국어로 말결 TM의 AI 상담원이라고 짧게 인사하고 어떤 문의인지 물어보세요.",
      }});
    };
    channel.onclose = channel.onerror = () => {
      if (active === call) finish("음성 연결이 종료됐어요. 다시 시작할 수 있어요.");
    };
    channel.onmessage = event => {
      if (active !== call) return;
      const data = JSON.parse(event.data);
      if (data.type === "response.done") handleResponse(call, data.response);
      if (data.type === "error") state.textContent = "AI가 요청을 처리하지 못했어요. 다시 말씀해 주세요.";
    };
    return peer;
  }

  async function begin() {
    if (active || finishing || start.disabled) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection) {
      state.textContent = "마이크를 지원하는 브라우저에서 HTTPS로 접속해 주세요.";
      return;
    }
    const call = {controller: new AbortController(), handled: new Set()};
    active = call;
    consent.disabled = true;
    recordingState.hidden = true;
    receipt.hidden = true;
    start.hidden = true;
    stop.hidden = false;
    stop.focus();
    state.textContent = "마이크 사용을 허용해 주세요.";
    try {
      if (consent.checked) {
        call.recorder = new VoiceRecording(() => {
          recordingState.textContent = "녹취 저장이 중단됐어요. 서버에 도착한 부분까지만 보관돼요.";
        });
        await call.recorder.prepare(dialog.dataset.processorUrl);
        if (active !== call) return;
      }
      const stream = await navigator.mediaDevices.getUserMedia({audio: true});
      if (active !== call) {
        stream.getTracks().forEach(track => track.stop());
        return;
      }
      call.stream = stream;
      state.textContent = "AI 음성 상담에 연결하고 있어요.";
      call.timer = setTimeout(() => finish("연결 시간이 초과됐어요. 다시 시작해 주세요."), 25000);
      const peer = connectMedia(call);
      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      const signal = call.controller.signal;
      const record = Boolean(call.recorder);
      const access = await api(sessions, {method: "POST", body: {recording_consent: record, summary_consent: record}, signal});
      call.id = access.voice_id;
      if (active !== call) { releaseSession(call.id).catch(() => {}); return; }
      const answer = await fetch("https://api.openai.com/v1/realtime/calls", {
        method: "POST", credentials: "omit", signal,
        headers: {"Authorization": "Bearer " + access.client_secret, "Content-Type": "application/sdp"},
        body: offer.sdp,
      });
      if (!answer.ok) throw new Error("AI가 음성 연결을 수락하지 않았어요. 다시 시작해 주세요.");
      await peer.setRemoteDescription({type: "answer", sdp: await answer.text()});
    } catch (error) {
      if (active !== call) return;
      const message = {
        NotAllowedError: "브라우저에서 마이크 사용을 허용한 뒤 다시 시작해 주세요.",
        NotFoundError: "사용할 마이크를 찾지 못했어요. 마이크 연결을 확인해 주세요.",
        NotReadableError: "마이크를 열 수 없어요. 장치 설정을 확인해 주세요.",
        TypeError: "음성 서비스에 연결할 수 없어요. 네트워크를 확인해 주세요.",
      };
      finish(message[error.name] || error.message || "음성 연결을 시작할 수 없어요.");
    }
  }

  document.getElementById("voice-open").addEventListener("click", () => dialog.showModal());
  start.addEventListener("click", begin);
  stop.addEventListener("click", () => finish("음성 상담이 종료됐어요."));
  dialog.addEventListener("close", () => finish("음성 상담이 종료됐어요."));
  window.addEventListener("pagehide", () => finish("음성 상담이 종료됐어요."));
})();
