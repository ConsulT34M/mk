// One fixed KST snapshot for both role previews.
const sampleDay = "2026-11-25";
const at = time => Date.parse(sampleDay + "T" + (time.length === 5 ? time + ":00" : time) + "+09:00") / 1000;
const asOf = at("14:35");
const states = {queued: "배정 대기", active: "진행 중", closed: "종결"};
const initialTickets = [
  // ID, channel, intake, first acceptance, close, current owner, most recent requeue
  ["A7F04B21", "웹채팅", "14:32", null, null, ""],
  ["C83D217E", "전화", "14:26", "14:26:30", null, "이수민"],
  ["B601FE53", "웹채팅", "14:18", null, null, ""],
  ["D9243AC8", "웹채팅", "14:10", "14:10:20", null, "박지안"],
  ["E140B8F6", "전화", "13:55", "13:56", "14:07", "김도윤"],
  ["F7685DA2", "전화", "13:48", "13:48:45", null, "", "14:31"],
  ["8BD350E1", "전화", "13:30", "13:30:25", null, "김도윤"],
  ["709EAD42", "웹채팅", "13:21", "13:21:35", null, "이수민"],
  ["61D32FA5", "웹채팅", "12:52", "12:52:45", "13:04", "박지안"],
  ["053A7EC2", "전화", "11:47", "11:47:40", "11:56", "이수민"],
  ["3EAC674B", "웹채팅", "10:29", "10:29:30", "10:41", "박지안"],
  ["4BA19E7C", "웹채팅", "09:51", "09:51:20", "10:03", "이수민"]
].map(([id, channel, time, firstAssigned, closedAt, owner, requeued]) => ({
  id, channel, time, firstAssigned, closedAt, owner, queueSince: requeued || time,
  status: closedAt ? "closed" : owner ? "active" : "queued"
}));
const initialAgents = [
  {name: "이수민", login: "sumin.lee", goals: {daily: 4, weekly: 20, monthly: 80}},
  {name: "박지안", login: "jian.park", goals: {daily: 5, weekly: 25, monthly: 100}},
  {name: "김도윤", login: "doyun.kim", goals: {daily: 3, weekly: 15, monthly: 60}}
];
const voiceSessions = [
  {ticketId: "C83D217E"}, {ticketId: null}, {ticketId: "F7685DA2"}, {ticketId: null}
];
const byId = id => document.getElementById(id);
const periods = {daily: "일간", weekly: "주간", monthly: "월간"};

function loadSample(key, fallback) {
  try { return JSON.parse(localStorage.getItem("malgyeol.preview." + key)) ?? fallback; }
  catch { return fallback; }
}
function saveSample(key, value) {
  try { localStorage.setItem("malgyeol.preview." + key, JSON.stringify(value)); return true; }
  catch { return false; }
}
const inquiryColors = {"계정·로그인": "#6459b4", "입출금": "#dbb559", "거래": "#9e91d5", "이용 안내": "#78a5a0"};
// Today's cases retain their IDs and ownership when an older preview is reopened.
const todayTopics = {
  A7F04B21: ["계정·로그인", "로그인 알림 설정"], C83D217E: ["입출금", "출금 요청 진행 상태"],
  B601FE53: ["계정·로그인", "로그인 인증 메일 재발송"], D9243AC8: ["거래", "주문 체결 내역 확인"],
  E140B8F6: ["계정·로그인", "등록 연락처 변경"], F7685DA2: ["입출금", "입금 반영 지연"],
  "8BD350E1": ["입출금", "출금 처리 시간"], "709EAD42": ["이용 안내", "채팅 창 다시 열기"],
  "61D32FA5": ["거래", "주문 내역 조회"], "053A7EC2": ["계정·로그인", "로그인 인증 결과 확인"],
  "3EAC674B": ["거래", "거래 알림 수신 방법"], "4BA19E7C": ["이용 안내", "문의 접수 확인"]
};
const historyCases = [
  // Date, ticket, owner, channel, first connection, end, inquiry type, subject, outcome.
  ["2026-11-24", "24A01D71", "sumin.lee", "전화", "15:08", "15:15", "입출금", "입금 내역 확인", "해결"],
  ["2026-11-24", "24B02E82", "jian.park", "웹채팅", "11:22", "11:30", "거래", "거래 내역 출력", "해결"],
  ["2026-11-24", "24C03F93", "doyun.kim", "전화", "10:13", "10:18", "계정·로그인", "인증 수단 변경", "후속 안내"],
  ["2026-11-23", "23A01D71", "sumin.lee", "웹채팅", "13:10", "13:21", "입출금", "출금 한도 문의", "후속 안내"],
  ["2026-11-23", "23B02E82", "jian.park", "전화", "10:12", "10:24", "거래", "주문 정정 확인", "해결"],
  ["2026-11-20", "20A01D71", "sumin.lee", "웹채팅", "14:03", "14:13", "거래", "거래 내역 찾기", "해결"],
  ["2026-11-20", "20B02E82", "jian.park", "웹채팅", "09:46", "09:53", "입출금", "입금 반영 시간", "해결"],
  ["2026-11-12", "12A01D71", "sumin.lee", "전화", "11:06", "11:12", "입출금", "출금 결과 안내", "후속 안내"],
  ["2026-11-09", "09C03F93", "doyun.kim", "웹채팅", "10:30", "10:40", "이용 안내", "상담 가능 시간", "해결"],
  ["2026-10-30", "30A01D71", "sumin.lee", "웹채팅", "15:40", "15:46", "계정·로그인", "비밀번호 재설정", "해결"],
  ["2026-10-29", "29C03F93", "doyun.kim", "전화", "14:15", "14:22", "입출금", "입금 내역 조회", "해결"]
];
const recordings = {"053A7EC2": "sample-login.mp3", "24A01D71": "sample-deposit.mp3", E140B8F6: "sample-contact.mp3"};
const outboundCalls = new Set(["053A7EC2", "12A01D71", "23B02E82"]);
const snapshotTime = "14:35";
function initialDesk() {
  const legacy = loadSample("goals", {}) || {};
  const agents = initialAgents.map(agent => {
    const stored = legacy[agent.login] || {};
    const goals = {...agent.goals};
    for (const key of Object.keys(periods)) {
      const n = stored[key];
      if (n === null || (Number.isInteger(n) && n >= 0 && n <= 99999)) goals[key] = n;
    }
    return {...agent, loginId: stored.loginId || agent.login, enabled: true, goals};
  });
  const tickets = initialTickets.map(item => ({...item, ownerId: agents.find(agent => agent.name === item.owner)?.login || null}));
  return {agents, tickets, changes: []};
}
function readDesk() {
  const state = loadSample("desk", null) || initialDesk();
  if (state.version === 4) return state;
  state.teams = [{id: "support", name: "고객지원팀"}, {id: "operations", name: "운영지원팀"}];
  for (const agent of state.agents) {
    agent.teamId = {"sumin.lee": "support", "jian.park": "support", "doyun.kim": "operations"}[agent.login] || null;
  }
  for (const item of state.tickets) {
    const [category, subject] = todayTopics[item.id];
    Object.assign(item, {date: sampleDay, category, subject});
    if (item.closedAt) item.outcome = item.id === "3EAC674B" ? "후속 안내" : "해결";
  }
  for (const [date, id, ownerId, channel, time, closedAt, category, subject, outcome] of historyCases) {
    const owner = state.agents.find(agent => agent.login === ownerId)?.name || "";
    state.tickets.push({id, date, channel, time, firstAssigned: time, closedAt, category, subject, outcome, ownerId, owner, queueSince: time, status: "closed"});
  }
  state.notices = [
    {id: "notice-25", title: "출금 지연 문의 응대 안내", body: "출금 지연 문의를 받으면 먼저 요청 시각과 처리 상태를 확인해 주세요. 추가 확인이 필요한 건은 상담 메모에 남겨 다음 담당자가 이어서 안내할 수 있도록 해 주세요.", date: sampleDay, time: "09:10"},
    {id: "notice-23", title: "상담 인계 시 메모 작성 안내", body: "상담을 인계할 때는 고객이 문의한 내용과 이미 안내한 내용을 나눠 적어 주세요.", date: "2026-11-23", time: "10:00"}
  ];
  state.version = 4;
  return state;
}
let desk = readDesk();
function todayTickets() { return desk.tickets.filter(item => item.date === sampleDay); }
function teamTickets(teamId) {
  if (!teamId) return [];
  const members = new Set(desk.agents.filter(agent => agent.teamId === teamId).map(agent => agent.login));
  return desk.tickets.filter(item => members.has(item.ownerId));
}
function orderedQueue() {
  return desk.tickets.filter(item => item.status === "queued").sort((a, b) => at(a.queueSince) - at(b.queueSince) || a.id.localeCompare(b.id));
}
// Apply a whole preview action, including ticket reassignments, as one browser-storage update.
async function updateDesk(change) {
  function commit() {
    desk = readDesk();
    const error = change(desk);
    if (error) return error;
    if (!saveSample("desk", desk)) {
      desk = readDesk();
      return "이 브라우저에 변경 내용을 저장할 수 없습니다.";
    }
  }
  return navigator.locks ? navigator.locks.request("malgyeol.preview.desk", commit) : commit();
}
function onDeskChange(render) {
  function read() { desk = readDesk(); render(); }
  window.addEventListener("storage", event => { if (!event.key || event.key === "malgyeol.preview.desk") read(); });
  window.addEventListener("pageshow", event => { if (event.persisted) read(); });
}
function htmlText(value) {
  const element = document.createElement("span");
  element.textContent = value;
  return element.innerHTML;
}
function duration(seconds) {
  if (seconds === null) return "—";
  const whole = Math.round(seconds);
  const minutes = Math.floor(whole / 60);
  return minutes ? minutes + "분" + (whole % 60 ? " " + whole % 60 + "초" : "") : whole + "초";
}
function channelStats(list) {
  const waiting = list.filter(item => item.status === "queued");
  const answered = list.filter(item => item.firstAssigned);
  const waitSum = answered.reduce((sum, item) => sum + at(item.firstAssigned) - at(item.time), 0);
  return {
    completed: list.filter(item => item.closedAt).length,
    queued: waiting.length,
    longest: waiting.reduce((longest, item) => Math.max(longest, asOf - at(item.queueSince)), 0),
    asa: answered.length ? waitSum / answered.length : null
  };
}
function setupNavigation(render, aliases = {}) {
  const links = [...document.querySelectorAll(".nav-link")];
  function show() {
    const hash = location.hash.slice(1);
    const view = Object.hasOwn(aliases, hash) ? aliases[hash] : hash;
    const current = links.find(link => link.hash.slice(1) === view) || links[0];
    const selected = current.hash.slice(1);
    for (const link of links) {
      if (link === current) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    for (const region of document.querySelectorAll("[data-views]")) {
      region.hidden = !region.dataset.views.split(" ").includes(selected);
    }
    byId("view-title").textContent = current.dataset.title || current.textContent.trim();
    document.title = byId("view-title").textContent + " 시안 · 말결 TM";
    render(selected);
  }
  document.querySelector(".skip").addEventListener("click", event => {
    event.preventDefault();
    byId("content").focus();
  });
  window.addEventListener("hashchange", () => {
    show();
    byId("view-title").focus({preventScroll: true});
    window.scrollTo(0, 0);
  });
  show();
}
