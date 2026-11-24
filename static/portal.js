"use strict";

const $ = (selector, parent = document) => parent.querySelector(selector);
const notice = $("#notice");
let noticeTimer;

function tell(message) {
  notice.textContent = message;
  notice.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { notice.hidden = true; }, 7000);
}

async function api(path, { method = "GET", body, actor } = {}) {
  const headers = {};
  if (actor) headers["X-Chat-Actor"] = actor;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const result = await fetch(path, { method, headers, body, credentials: "same-origin", cache: "no-store" });
  if (result.status === 204) return null;
  const data = await result.json().catch(() => ({}));
  if (!result.ok) {
    const error = new Error(data.message || "요청을 완료하지 못했습니다. 다시 시도해 주세요.");
    error.status = result.status;
    throw error;
  }
  return data;
}

const dateFormat = new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "short" });
document.querySelectorAll("time[datetime]").forEach(el => {
  el.textContent = dateFormat.format(new Date(el.dateTime));
});
document.querySelectorAll("[data-datetime]").forEach(el => {
  if (!el.dataset.datetime) return;
  const date = new Date(el.dataset.datetime);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  el.value = local.toISOString().slice(0, 16);
});

$("[data-logout]")?.addEventListener("click", async () => {
  try {
    await api("/staff/sessions/logout", { method: "POST" });
    location.assign("/");
  } catch (error) {
    if (error.status === 401) location.assign("/");
    else tell(error.message);
  }
});

const filters = document.querySelectorAll("[data-filter]");
filters.forEach(button => button.addEventListener("click", () => {
  let count = 0;
  filters.forEach(item => item.setAttribute("aria-pressed", String(item === button)));
  document.querySelectorAll("[data-case-status]").forEach(row => {
    row.hidden = button.dataset.filter !== "ALL" && row.dataset.caseStatus !== button.dataset.filter;
    if (!row.hidden) count++;
  });
  $("#empty-list").hidden = count > 0;
}));

const chat = $("[data-chat]");
let lastHistory = "";
let chatStopped = false;

function showMessages(items) {
  const list = $("[data-messages]", chat);
  const signature = items.length + ":" + (items.at(-1)?.message_id || "");
  if (lastHistory === signature) return;
  const nearEnd = !lastHistory || list.scrollHeight - list.scrollTop - list.clientHeight < 60;
  lastHistory = signature;
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const mine = (chat.dataset.actor === "staff") === (item.author_role === "AGENT");
    const wrap = document.createElement("div");
    wrap.className = mine ? "message mine" : "message";
    const label = document.createElement("small");
    label.textContent = (item.author_role === "AGENT" ? "상담원" : "고객") + " · " + dateFormat.format(new Date(item.sent_at));
    const bubble = document.createElement("p");
    bubble.className = "bubble";
    bubble.textContent = item.body;
    wrap.append(label, bubble);
    fragment.append(wrap);
  }
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "표시할 메시지가 없습니다.";
    fragment.append(empty);
  }
  list.replaceChildren(fragment);
  if (nearEnd) list.scrollTop = list.scrollHeight;
}

async function refreshChat() {
  if (!chat || chatStopped) return;
  const hint = $("[data-chat-hint]", chat);
  const state = $("[data-chat-state]", chat);
  const controls = $(".chat-compose", chat).elements;
  try {
    const data = await api("/cases/" + chat.dataset.id + "/messages", { actor: chat.dataset.actor });
    showMessages(data.items);
    state.textContent = data.read_only ? "종결" : "상담 중";
    hint.textContent = data.read_only ? "종결된 상담은 읽기 전용입니다." : "";
    for (const control of controls) control.disabled = data.read_only;
  } catch (error) {
    hint.textContent = error.message;
    state.textContent = "연결 확인";
    if ([401, 403, 404].includes(error.status)) {
      chatStopped = true;
      $("[data-messages]", chat).replaceChildren();
      for (const control of controls) control.disabled = true;
    }
  }
}

if (chat) {
  async function poll() {
    await refreshChat();
    if (!chatStopped) setTimeout(poll, 2000);
  }
  poll();
}

document.addEventListener("submit", async event => {
  const form = event.target;
  const action = form.dataset.action;
  if (!action) return;
  event.preventDefault();
  if (form.dataset.busy) return;
  const data = new FormData(form);
  const values = Object.fromEntries(data);
  const path = "/staff/cases/" + form.dataset.id;
  const button = $("button[type=submit]", form);
  form.dataset.busy = "1";
  button.disabled = true;
  try {
    switch (action) {
      case "login":
        await api("/staff/sessions", { method: "POST", body: values });
        location.assign("/portal");
        break;
      case "new-phone":
        await api("/staff/phone-cases", { method: "POST", body: {} });
        location.reload();
        break;
      case "assign":
        await api(path + "/assignee", { method: "POST", body: values });
        location.reload();
        break;
      case "phone": {
        const update = Object.fromEntries(Object.entries(values).filter(([, value]) => value !== ""));
        if (update.call_at) update.call_at = new Date(update.call_at).toISOString();
        if ("followup_required" in update) update.followup_required = update.followup_required === "yes";
        await api(path + "/phone-response", { method: "POST", body: update });
        location.reload();
        break;
      }
      case "close":
        if (!window.confirm("상담을 종결하면 기록이 읽기 전용으로 바뀝니다. 종결할까요?")) break;
        await api(path + "/close", { method: "POST", body: values });
        location.reload();
        break;
      case "message": {
        const actor = chat.dataset.actor;
        if (form.dataset.lastBody !== values.body) {
          form.dataset.requestId = crypto.randomUUID();
          form.dataset.lastBody = values.body;
        }
        await api("/cases/" + form.dataset.id + "/messages", {
          method: "POST", actor, body: { client_message_id: form.dataset.requestId, body: values.body },
        });
        form.reset();
        delete form.dataset.lastBody;
        await refreshChat();
        break;
      }
      case "upload":
        form.dataset.requestId ||= crypto.randomUUID();
        data.set("upload_request_id", form.dataset.requestId);
        await api(path + "/recordings", { method: "POST", body: data });
        location.reload();
        break;
    }
  } catch (error) {
    tell(error.message);
  } finally {
    delete form.dataset.busy;
    if (action !== "message" || (!chatStopped && !$("#chat-message").disabled)) button.disabled = false;
  }
});

document.querySelectorAll("input[type=file]").forEach(input => input.addEventListener("change", () => {
  delete input.form.dataset.requestId;
}));
document.querySelectorAll("audio").forEach(audio => audio.addEventListener("error", () => {
  tell("녹취를 재생할 수 없습니다. 배정 상태와 보관 기한을 확인해 주세요.");
}));
