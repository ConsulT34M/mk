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

async function api(path, { method = "GET", body, actor, signal, keepalive = false } = {}) {
  const headers = {};
  if (actor) headers["X-Chat-Actor"] = actor;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const result = await fetch(path, { method, headers, body, signal, keepalive, credentials: "same-origin", cache: "no-store" });
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
const ticketClock = new Intl.DateTimeFormat("ko-KR", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
function formatTimes(root) {
  root.querySelectorAll("time[datetime]").forEach(el => {
    const date = new Date(el.dateTime);
    const fullDate = dateFormat.format(date);
    if (el.hasAttribute("data-ticket-time")) {
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      el.textContent = month + "." + day + " " + ticketClock.format(date);
      el.title = fullDate;
    } else {
      el.textContent = fullDate;
    }
  });
}
formatTimes(document);
document.querySelectorAll("[data-datetime]").forEach(el => {
  if (!el.dataset.datetime) return;
  const date = new Date(el.dataset.datetime);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  el.value = local.toISOString().slice(0, 16);
});
document.querySelectorAll("form[data-phone-fields]").forEach(form => {
  for (const [name, value] of new FormData(form)) form.elements.namedItem(name).dataset.original = value;
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

const chat = $("[data-chat]");
const inquiryChat = $("#inquiry-chat");
let lastHistory = "";
let chatStopped = false;
let chatRead = 0;

function showMessages(items) {
  const list = $("[data-messages]", chat);
  const signature = JSON.stringify(items);
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
    empty.textContent = inquiryChat ? "아직 대화가 없어요." : "표시할 메시지가 없습니다.";
    fragment.append(empty);
  }
  list.replaceChildren(fragment);
  if (nearEnd) list.scrollTop = list.scrollHeight;
}

async function refreshChat() {
  if (!chat || chatStopped || (inquiryChat && !inquiryChat.open)) return;
  const read = ++chatRead;
  const hint = $("[data-chat-hint]", chat);
  const state = $("[data-chat-state]", chat);
  const controls = $(".chat-compose", chat).elements;
  try {
    const data = await api("/cases/" + chat.dataset.id + "/messages", { actor: chat.dataset.actor });
    if (read !== chatRead) return;
    showMessages(data.items);
    if (inquiryChat) {
      state.dataset.status = data.status;
      state.textContent = {QUEUED: "상담원 연결 대기", IN_PROGRESS: "상담원 연결됨", CLOSED: "상담 종료"}[data.status];
      hint.textContent = data.status === "QUEUED" ? "상담원이 연결되기 전에도 문의를 남길 수 있어요." :
        data.read_only ? "종료된 상담이에요. 이전 대화를 확인할 수 있어요." : "";
      $("[data-new-inquiry]", chat).hidden = !data.read_only;
    } else {
      state.textContent = data.read_only ? "종결" : "상담 중";
      hint.textContent = data.read_only ? "종결된 상담은 읽기 전용입니다." : "";
    }
    for (const control of controls) control.disabled = data.read_only || (control.type === "submit" && !!control.form.dataset.busy);
  } catch (error) {
    if (read !== chatRead) return;
    hint.textContent = error.message;
    state.textContent = "연결 확인";
    if ([401, 403, 404].includes(error.status)) {
      chatStopped = true;
      $("[data-messages]", chat).replaceChildren();
      for (const control of controls) control.disabled = true;
      if (inquiryChat) {
        hint.textContent = "이 상담의 연결이 끝났어요. 문의 페이지에서 다시 시작해 주세요.";
        $("[data-new-inquiry]", chat).hidden = true;
        $("[data-reconnect]", chat).hidden = false;
      }
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

if (inquiryChat) {
  const launcher = $("#open-inquiry button");
  launcher.form.addEventListener("submit", event => {
    if (chatStopped) return;
    event.preventDefault();
    if (!inquiryChat.open) inquiryChat.show();
    launcher.setAttribute("aria-expanded", "true");
    history.replaceState(null, "", "?chat=open");
    ($("#chat-message").disabled ? $("#minimize-inquiry") : $("#chat-message")).focus();
    refreshChat();
  });
  function minimize() {
    inquiryChat.close();
    launcher.setAttribute("aria-expanded", "false");
    history.replaceState(null, "", location.pathname);
    launcher.focus();
  }
  $("#minimize-inquiry").addEventListener("click", minimize);
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && inquiryChat.open && !event.isComposing) minimize();
  });
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
  const button = event.submitter || $("button[type=submit], button:not([type])", form);
  form.dataset.busy = "1";
  button.disabled = true;
  try {
    switch (action) {
      case "login": {
        const staff = await api("/staff/sessions", { method: "POST", body: values });
        location.assign(staff.destination);
        break;
      }
      case "admin": {
        if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) break;
        if (form.hasAttribute("data-phone-fields")) {
          for (const key of Object.keys(values)) {
            if (values[key] === form.elements.namedItem(key).dataset.original) delete values[key];
            else if (values[key] === "") values[key] = null;
          }
          if (!Object.keys(values).length) { tell("수정할 내용이 없습니다."); break; }
          if (values.call_at) values.call_at = new Date(values.call_at).toISOString();
          if (values.followup_required) values.followup_required = values.followup_required === "yes";
        }
        if (form.hasAttribute("data-team-form")) {
          values.members = data.getAll("members");
          values.previous = JSON.parse(values.previous);
        }
        if (form.hasAttribute("data-ai-policy")) {
          values.enabled = data.has("enabled");
          values.offer_handoff = data.has("offer_handoff");
        }
        const saved = await api(form.action, { method: "POST", body: values });
        sessionStorage.setItem("adminNotice", form.dataset.done || "저장했습니다.");
        if (form.hasAttribute("data-team-form")) location.assign("/admin?tab=org&team=" + encodeURIComponent(saved.team_id));
        else if (form.dataset.return) location.assign(form.dataset.return);
        else location.reload();
        break;
      }
      case "case-field":
        await api(form.action, {method: "POST", body: values});
        tell("문의 유형을 저장했습니다.");
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
        if (form.elements.body.value === values.body) form.reset();
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
    if (action === "admin" && error.status === 401) location.assign("/");
    else tell(error.message);
  } finally {
    delete form.dataset.busy;
    if (action !== "message" || (!chatStopped && !$("#chat-message").disabled)) button.disabled = false;
  }
});

if (document.querySelector(".admin-desk")) {
  window.addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
  const message = sessionStorage.getItem("adminNotice");
  if (message) {
    sessionStorage.removeItem("adminNotice");
    tell(message);
  }
}

const adminDashboard = $("#admin-dashboard");
if (adminDashboard) {
  const syncStatus = $("#admin-sync-status");
  let loading = false;
  async function refreshDashboard() {
    if (loading || document.hidden) return;
    loading = true;
    try {
      const response = await fetch(location.href, { cache: "no-store" });
      if (response.redirected || response.status === 403) { location.replace("/"); return; }
      if (!response.ok) throw new Error();
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const next = page.getElementById("admin-dashboard");
      if (!next) throw new Error();
      syncStatus.textContent = "";
      // Keep an administrator's pending choice intact when another ticket changes.
      const choosing = [...adminDashboard.querySelectorAll(".assign-form")].some(form =>
        form.elements.new_agent_id.value !== form.elements.previous_agent_id.value);
      const filterChanged = [...adminDashboard.querySelectorAll(".report-filter select, .report-filter input:not([type=hidden])")].some(field => {
        if (field.tagName !== "SELECT") return field.value !== field.defaultValue;
        const initial = [...field.options].find(option => option.defaultSelected) || field.options[0];
        return field.value !== initial.value;
      });
      if (choosing || filterChanged || adminDashboard.contains(document.activeElement) || adminDashboard.querySelector("[data-busy]")) return;
      formatTimes(next);
      if (!adminDashboard.isEqualNode(next)) adminDashboard.replaceChildren(...next.childNodes);
    } catch {
      syncStatus.textContent = "대시보드 연결 중…";
    } finally {
      loading = false;
    }
  }
  setInterval(refreshDashboard, 5000);
  document.addEventListener("visibilitychange", refreshDashboard);
}

document.querySelectorAll("input[type=file]").forEach(input => input.addEventListener("change", () => {
  delete input.form.dataset.requestId;
}));
document.querySelectorAll("audio").forEach(audio => audio.addEventListener("error", () => {
  tell("녹취를 재생할 수 없습니다. 배정 상태와 보관 기한을 확인해 주세요.");
}));
