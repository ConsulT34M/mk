// Reports in both previews use the same dates, cases and membership.
function daysBeforeSample(days) {
  const date = new Date(sampleDay + "T00:00:00Z");
  date.setUTCDate(date.getUTCDate() - days);
  return date.toISOString().slice(0, 10);
}
function shortDate(date) { return date.replaceAll("-", "."); }
function callSeconds(item) { return at(item.closedAt) - at(item.assignedAt || item.firstAssigned); }
function percentage(count, total) { return total ? Math.round(count / total * 1000) / 10 + "%" : "0%"; }

function drawDistribution(root, rows, ring = false) {
  const total = rows.reduce((sum, row) => sum + row.count, 0);
  if (!total) { root.innerHTML = '<p class="empty">이 기간에 접수된 상담이 없습니다.</p>'; return; }
  let next = 0;
  const stops = rows.filter(row => row.count).map(row => {
    const start = next;
    next += row.count / total * 100;
    return row.color + " " + start + "% " + next + "%";
  });
  const center = ring ? '<span><b>' + total + '</b>건</span>' : "";
  root.innerHTML = `<div class="distribution"><div class="circle-chart ${ring ? "ring-chart" : ""}" aria-hidden="true" style="background:conic-gradient(${stops.join(",")})">${center}</div>
    <ul class="chart-key">${rows.map(row => `<li><span><i style="background:${row.color}"></i>${row.label}</span><b>${row.count}건</b><small>${percentage(row.count, total)}</small></li>`).join("")}</ul></div>`;
}

function dateRangeForm() {
  return `<form class="date-range"><label>시작일<input class="control" name="from" type="date" value="${daysBeforeSample(6)}" max="${sampleDay}" required></label>
    <span aria-hidden="true">–</span><label>종료일<input class="control" name="to" type="date" value="${sampleDay}" max="${sampleDay}" required></label><button class="page-btn">조회</button></form>`;
}
function setupRange(root, render) {
  const form = root.querySelector(".date-range");
  const range = {from: form.elements.from.value, to: form.elements.to.value};
  form.addEventListener("input", () => form.elements.to.setCustomValidity(""));
  form.addEventListener("submit", event => {
    event.preventDefault();
    const {from, to} = form.elements;
    to.setCustomValidity(from.value > to.value ? "종료일을 시작일 이후로 선택해 주세요." : "");
    if (!form.reportValidity()) return;
    Object.assign(range, {from: from.value, to: to.value});
    render();
  });
  return range;
}
function reportTable(root, headers, rows, total, empty) {
  root.innerHTML = `<table><thead><tr>${headers.map(label => "<th scope='col'>" + label + "</th>").join("")}</tr></thead><tbody>${
    rows.map(row => "<tr>" + row.map(cell => "<td>" + htmlText(String(cell)) + "</td>").join("") + "</tr>").join("") ||
    '<tr><td class="empty" colspan="' + headers.length + '">' + empty + '</td></tr>'}</tbody>${
    rows.length ? '<tfoot><tr><th scope="row">기간 합계</th>' + total.map(cell => "<td>" + cell + "</td>").join("") + "</tr></tfoot>" : ""}</table>`;
}
function createStatistics(root, getScope) {
  const tabs = {"activity": "처리 현황", "topics": "문의 유형", "outcomes": "처리 결과", "duration": "통화 시간"};
  root.innerHTML = `<div class="report-tools">${dateRangeForm()}<p class="report-caption" aria-live="polite"></p></div>
    <div class="report-tabs" role="group" aria-label="통계 항목">${Object.entries(tabs).map(([key, label]) => `<button type="button" data-report="${key}">${label}</button>`).join("")}</div><div class="table-wrap report-table"></div>`;
  let tab = "activity";
  const range = setupRange(root, render);
  function render() {
    const scope = getScope();
    const records = scope.tickets.filter(item => item.closedAt && item.date >= range.from && item.date <= range.to);
    root.querySelectorAll("[data-report]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.report === tab)));
    const byDuration = tab === "duration";
    const items = byDuration ? records.filter(item => item.channel === "전화") : records;
    root.querySelector(".report-caption").textContent = scope.label + (byDuration ? " · 종결 통화 " : " · 종결 상담 ") + items.length + "건";
    const groupKey = {activity: "date", duration: "date", topics: "category", outcomes: "outcome"}[tab];
    const groups = new Map();
    for (const item of items) {
      const key = item[groupKey];
      if (!groups.has(key)) groups.set(key, {calls: 0, chats: 0, inbound: 0, outbound: 0});
      const group = groups.get(key);
      if (item.channel === "전화") {
        group.calls++;
        group[outboundCalls.has(item.id) ? "outbound" : "inbound"] += callSeconds(item);
      } else group.chats++;
    }
    const ordered = [...groups.entries()].sort(([a, x], [b, y]) => groupKey === "date" ? b.localeCompare(a) : (y.calls + y.chats) - (x.calls + x.chats) || a.localeCompare(b, "ko"));
    const totals = {calls: 0, chats: 0, inbound: 0, outbound: 0};
    for (const group of groups.values()) for (const key of Object.keys(totals)) totals[key] += group[key];
    function values(group) {
      return byDuration ? [duration(group.inbound), duration(group.outbound), duration(group.inbound + group.outbound)] :
        [group.calls + "건", group.chats + "건", (group.calls + group.chats) + "건"];
    }
    const rows = ordered.map(([key, sums]) => [groupKey === "date" ? shortDate(key) : key, ...values(sums)]);
    const headers = byDuration ? ["날짜", "수신 시간", "발신 시간", "총 통화 시간"] : [groupKey === "date" ? "날짜" : tabs[tab], "통화", "채팅", "합계"];
    reportTable(root.querySelector(".report-table"), headers, rows, values(totals), byDuration ? "선택한 기간에 종결된 통화가 없습니다." : "선택한 기간에 종결된 상담이 없습니다.");
  }
  root.querySelector(".report-tabs").addEventListener("click", event => {
    const button = event.target.closest("[data-report]");
    if (button) { tab = button.dataset.report; render(); }
  });
  return render;
}

function createRecordings(root, getTickets, showOwner = false) {
  root.innerHTML = `<div class="report-tools">${dateRangeForm()}<p class="report-caption" aria-live="polite"></p></div>
    <div class="record-player" hidden><div><small>예시 녹취 발췌</small><strong></strong><span role="status"></span></div><audio controls preload="none" aria-label="선택한 통화 녹취"></audio></div>
    <div class="table-wrap"><table class="record-table"><thead><tr><th scope="col">통화 일시</th><th scope="col">문의 내용</th>${showOwner ? '<th scope="col">상담원</th>' : ""}<th scope="col">구분</th><th scope="col">통화 시간</th><th scope="col">녹취</th></tr></thead><tbody></tbody></table></div>`;
  const player = root.querySelector(".record-player");
  const audio = player.querySelector("audio");
  let selectedId = null;
  const range = setupRange(root, render);
  function stop() {
    if (!selectedId) return;
    selectedId = null;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    player.hidden = true;
  }
  function render() {
    const rows = getTickets().filter(item => item.channel === "전화" && item.date >= range.from && item.date <= range.to)
      .sort((a, b) => b.date.localeCompare(a.date) || b.time.localeCompare(a.time));
    if (selectedId && !rows.some(item => item.id === selectedId)) stop();
    root.querySelector(".report-caption").textContent = "통화 " + rows.length + "건 · 녹취 " + rows.filter(item => recordings[item.id]).length + "건";
    root.querySelector("tbody").innerHTML = rows.map(item => `<tr><td class="rec-time">${shortDate(item.date)} <small>${item.time}</small></td><td class="rec-subject">${htmlText(item.subject)}</td>${showOwner ? '<td class="rec-owner">' + htmlText(item.owner || "미배정") + "</td>" : ""}
      <td class="rec-type">${outboundCalls.has(item.id) ? "발신" : "수신"}</td><td class="rec-duration">${item.closedAt ? duration(callSeconds(item)) : states[item.status]}</td>
      <td class="rec-play">${recordings[item.id] ? `<button type="button" class="page-btn listen" data-listen="${item.id}" aria-label="${htmlText(item.subject)} 녹취 듣기" aria-pressed="${item.id === selectedId}">▷ 듣기</button>` : '<span class="muted">녹취 없음</span>'}</td></tr>`).join("") ||
      '<tr><td colspan="' + (showOwner ? 6 : 5) + '" class="empty">선택한 기간에 통화가 없습니다.</td></tr>';
  }
  root.querySelector("tbody").addEventListener("click", event => {
    const button = event.target.closest("[data-listen]");
    if (!button) return;
    const item = getTickets().find(ticket => ticket.id === button.dataset.listen);
    if (!item || !recordings[item.id]) return;
    if (selectedId !== item.id || audio.error) audio.src = recordings[item.id];
    selectedId = item.id;
    player.hidden = false;
    player.querySelector("strong").textContent = item.subject + " · " + shortDate(item.date);
    player.querySelector("[role=status]").textContent = "";
    root.querySelectorAll("[data-listen]").forEach(link => link.setAttribute("aria-pressed", String(link === button)));
    audio.play().catch(error => { if (selectedId === item.id && error.name !== "AbortError") player.querySelector("[role=status]").textContent = "재생 버튼을 눌러 다시 들어 주세요."; });
  });
  return {render, stop};
}

function renderNotices(root, notices = desk.notices) {
  root.innerHTML = notices.map(notice => `<li><button type="button" data-notice="${notice.id}"><strong>${htmlText(notice.title)}</strong><time>${shortDate(notice.date)}</time></button></li>`).join("");
}
function setupNoticeReader() {
  const dialog = byId("notice-reader");
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-notice]");
    if (!button) return;
    const notice = desk.notices.find(item => item.id === button.dataset.notice);
    if (!notice) return;
    dialog.querySelector("h2").textContent = notice.title;
    dialog.querySelector("small").textContent = "운영 관리자 · " + shortDate(notice.date) + " " + notice.time;
    dialog.querySelector("p").textContent = notice.body;
    dialog.showModal();
  });
}
