"use strict";

// Fictional conversations for the design preview. Edits last only until reload.
const cases = [
  {
    id: "TM-1042", customer: "C-5802", name: "김*연", tone: "violet", state: "진행 중",
    subject: "인증 메일 수신 문의", category: "계정 · 로그인",
    memo: "스팸 메일함 확인 완료. 재전송 후 수신 여부를 확인할 예정입니다.",
    reply: "로그인 화면에서 인증 메일 재전송을 선택해 주세요. 새 메일이 도착하면 알려주시겠어요?",
    messages: [
      ["고객", "10:18", "안녕하세요. 로그인 인증 메일이 오지 않아요."],
      ["상담원", "10:19", "안녕하세요, 고객님. 스팸 메일함에도 인증 메일이 없는지 확인해 주시겠어요?"],
      ["고객", "10:21", "네, 스팸 메일함에도 없어요. 다시 받을 수 있을까요?"],
    ],
  },
  {
    id: "TM-1041", customer: "C-3619", name: "박*준", tone: "mint", state: "진행 중", saved: true,
    subject: "계정 정보 변경", category: "회원 정보",
    memo: "고객이 연락처를 변경할 수 있는 화면을 안내했습니다.",
    reply: "회원 정보 화면에서 변경할 항목을 선택해 주세요. 어느 단계에서 도움이 필요하신가요?",
    messages: [
      ["고객", "10:11", "등록한 연락처를 변경하고 싶어요."],
      ["상담원", "10:13", "회원 정보 화면을 열고 ‘연락처 변경’을 선택해 주세요."],
      ["고객", "10:16", "네, 변경 화면을 찾았어요."],
    ],
  },
  {
    id: "TM-1040", customer: "C-2064", name: "이*우", tone: "sand", state: "대기",
    subject: "알림 설정 문의", category: "서비스 이용", memo: "아직 작성된 메모가 없습니다.",
    reply: "어떤 알림을 변경하고 싶으신가요? 사용하시는 화면에 맞춰 안내해 드릴게요.",
    messages: [["고객", "10:08", "앱에서 알림을 끄는 방법을 알고 싶어요."]],
  },
  {
    id: "TM-1039", customer: "C-7280", name: "최*빈", tone: "blue", state: "진행 중",
    subject: "기기 변경 후 로그인", category: "계정 · 로그인",
    memo: "새 기기에서 표시되는 안내 문구를 확인 중입니다.",
    reply: "화면에 표시된 안내 문구를 알려주시면 다음 단계를 확인해 드릴게요.",
    messages: [
      ["고객", "09:50", "휴대폰을 바꾼 뒤 처음 로그인하려고 해요."],
      ["상담원", "09:54", "새 기기에서 어떤 안내가 보이는지 확인해 주시겠어요?"],
    ],
  },
  {
    id: "TM-1038", customer: "C-4108", name: "윤*아", tone: "rose", state: "종결",
    subject: "지난 상담 내역 조회", category: "고객센터",
    memo: "고객이 기존 상담 내역을 확인해 상담을 종결했습니다.",
    reply: "", messages: [
      ["고객", "09:32", "지난번 상담 내용을 다시 볼 수 있나요?"],
      ["상담원", "09:33", "고객센터의 ‘내 상담’에서 이전 대화를 선택해 주세요."],
      ["고객", "09:40", "찾았습니다. 감사합니다!"],
    ],
  },
];

const $ = id => document.getElementById(id);
const desk = $("desk");
const mobileLayout = window.matchMedia("(max-width: 690px)");
let active = cases[0];
let view = "all";
let state = "전체";

function avatar(node, item) {
  node.textContent = item.name[0];
  node.dataset.tone = item.tone;
}

function setQuick(open) {
  $("quick-panel").hidden = !open;
  $("show-quick").setAttribute("aria-expanded", String(open));
}

function setInfo(open) {
  desk.classList.toggle("info-open", open);
  $("shade").hidden = !open;
  $("show-info").setAttribute("aria-expanded", String(open));
  if (open) $("close-info").focus();
}

function renderList(items) {
  const list = $("case-list");
  list.replaceChildren();
  $("count").textContent = items.length;
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "조건에 맞는 상담이 없습니다.";
    list.append(empty);
    return;
  }
  for (const item of items) {
    const row = $("case-template").content.firstElementChild.cloneNode(true);
    const last = item.messages.at(-1);
    row.dataset.id = item.id;
    row.setAttribute("aria-pressed", String(item === active));
    avatar(row.querySelector(".avatar"), item);
    row.querySelector(".case-name").textContent = item.name;
    row.querySelector(".case-time").textContent = last[1];
    row.querySelector(".case-subject").textContent = item.subject;
    row.querySelector(".preview-line").textContent = last[2];
    list.append(row);
  }
}

function renderThread() {
  const item = active;
  const closed = item.state === "종결";
  avatar($("head-avatar"), item);
  $("head-name").textContent = item.name + " 고객";
  $("head-state").textContent = item.state;
  $("head-state").dataset.state = item.state;
  $("head-subject").textContent = item.subject;
  $("save-case").setAttribute("aria-pressed", String(Boolean(item.saved)));
  $("messages").replaceChildren();
  for (const [from, time, body] of item.messages) {
    const row = $("message-template").content.firstElementChild.cloneNode(true);
    row.classList.toggle("mine", from === "상담원");
    row.querySelector(".speaker").textContent = from === "상담원" ? "agent1 상담원" : item.name + " 고객";
    row.querySelector(".at").textContent = time;
    row.querySelector(".bubble").textContent = body;
    $("messages").append(row);
  }
  $("answer").value = item.draft || "";
  $("answer").disabled = closed;
  $("answer").placeholder = closed ? "종결된 상담입니다." : "고객에게 보낼 답변을 입력하세요";
  $("send").disabled = closed || !$("answer").value.trim();
  $("show-quick").disabled = closed;
  $("quick-topic").textContent = item.category;
  $("quick-text").textContent = item.reply;
  $("scroll-thread").scrollTop = $("scroll-thread").scrollHeight;
}

function renderInfo() {
  avatar($("info-avatar"), active);
  $("customer-name").textContent = active.name;
  $("customer-ref").textContent = "고객 번호 " + active.customer;
  $("case-ref").textContent = active.id;
  $("category").textContent = active.category;
  $("assignee").textContent = active.state === "대기" ? "미배정" : "agent1";
  $("memo").textContent = active.memo;
  const events = [["상담 접수", active.messages[0][1]]];
  const firstReply = active.messages.find(m => m[0] === "상담원");
  if (firstReply) events.push(["상담원 응대 시작", firstReply[1]]);
  if (active.state === "종결") events.push(["상담 종결", active.messages.at(-1)[1]]);
  $("history").replaceChildren();
  for (const [label, time] of events) {
    const li = document.createElement("li");
    const at = document.createElement("small");
    at.textContent = time;
    li.append(label, at);
    $("history").append(li);
  }
}

function render() {
  const query = $("search").value.trim().toLowerCase();
  const items = cases.filter(item => {
    if (view === "saved" && !item.saved) return false;
    if (view === "closed" && item.state !== "종결") return false;
    if (state !== "전체" && item.state !== state) return false;
    return [item.name, item.id, item.subject, item.category, ...item.messages.map(m => m[2])]
      .some(value => value.toLowerCase().includes(query));
  });
  if (!items.includes(active)) active = items[0];
  renderList(items);
  $("thread").hidden = $("detail-content").hidden = !active;
  $("thread-empty").hidden = $("detail-empty").hidden = Boolean(active);
  if (active) {
    renderThread();
    renderInfo();
  } else {
    desk.dataset.pane = "list";
  }
}

$("case-list").addEventListener("click", event => {
  const row = event.target.closest("[data-id]");
  if (!row) return;
  active = cases.find(item => item.id === row.dataset.id);
  desk.dataset.pane = "chat";
  setQuick(false);
  render();
  if (mobileLayout.matches) $("back").focus();
  else $("case-list").querySelector('[aria-pressed="true"]')?.focus({ preventScroll: true });
});

document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => {
  view = button.dataset.view;
  state = "전체";
  $("search").value = "";
  $("inbox-title").textContent = { all: "대화", saved: "즐겨찾기", closed: "종결 상담" }[view];
  $("filters").hidden = view === "closed";
  document.querySelectorAll("[data-view]").forEach(b => b.setAttribute("aria-pressed", String(b === button)));
  document.querySelectorAll("#filters button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.state === state)));
  desk.dataset.pane = "list";
  setQuick(false);
  render();
}));

$("filters").addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button) return;
  state = button.dataset.state;
  document.querySelectorAll("#filters button").forEach(b => b.setAttribute("aria-pressed", String(b === button)));
  setQuick(false);
  render();
});
$("search").addEventListener("input", () => { setQuick(false); render(); });
$("save-case").addEventListener("click", () => { active.saved = !active.saved; setQuick(false); render(); });
$("back").addEventListener("click", () => {
  desk.dataset.pane = "list";
  setQuick(false);
  $("case-list").querySelector('[aria-pressed="true"]')?.focus();
});
$("show-info").addEventListener("click", () => setInfo(true));
function closeInfo() { setInfo(false); $("show-info").focus(); }
$("close-info").addEventListener("click", closeInfo);
$("shade").addEventListener("click", closeInfo);
window.matchMedia("(max-width: 945px)").addEventListener("change", () => setInfo(false));
$("show-quick").addEventListener("click", () => setQuick($("quick-panel").hidden));
$("close-quick").addEventListener("click", () => { setQuick(false); $("show-quick").focus(); });
$("insert").addEventListener("click", () => {
  const input = $("answer");
  const draft = (input.value ? input.value + "\n" : "") + active.reply;
  input.value = draft.slice(0, input.maxLength);
  input.dispatchEvent(new Event("input"));
  setQuick(false);
  input.focus();
});
document.addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  if (desk.classList.contains("info-open")) closeInfo();
  else if (!$("quick-panel").hidden) { setQuick(false); $("show-quick").focus(); }
});

$("answer").addEventListener("input", event => {
  active.draft = event.target.value;
  $("send").disabled = !active.draft.trim();
});
$("answer").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});
$("composer").addEventListener("submit", event => {
  event.preventDefault();
  const text = $("answer").value.trim();
  if (!text || active.state === "종결") return;
  active.messages.push(["상담원", "방금", text]);
  active.draft = "";
  if (active.state === "대기") {
    active.state = "진행 중";
    if (state === "대기") {
      state = "진행 중";
      document.querySelectorAll("#filters button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.state === state)));
    }
  }
  setQuick(false);
  render();
  $("answer").focus();
});

render();
