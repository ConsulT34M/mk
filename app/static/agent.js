"use strict";

const desk = document.getElementById("desk");
const liveList = document.getElementById("live-list");
const search = document.getElementById("inbox-search");
const syncStatus = document.getElementById("sync-status");
const listURL = new URL("/portal/updates", location.origin);
listURL.searchParams.set("tab", desk.dataset.tab);
const listStatus = new URL(location.href).searchParams.get("status");
if (listStatus) listURL.searchParams.set("status", listStatus);
if (desk.dataset.selected) listURL.searchParams.set("case_id", desk.dataset.selected);
let previousHTML;
let previousQuota;
let previousNotes;
const lastBoard = {};
let refreshing = false;

function filterInbox() {
  if (!search) return;
  const query = search.value.trim().toLowerCase();
  const rows = liveList.querySelectorAll("[data-case-id]");
  let visible = 0;
  for (const row of rows) {
    row.hidden = !row.textContent.toLowerCase().includes(query) && !row.dataset.caseId.includes(query);
    if (!row.hidden) visible++;
  }
  document.getElementById("no-matches").hidden = !rows.length || visible > 0;
}
search?.addEventListener("input", filterInbox);

async function refreshDesk() {
  if (refreshing || document.hidden) return;
  refreshing = true;
  try {
    const data = await api(listURL);
    syncStatus.textContent = "";
    for (const [id,key] of [["live-chart","chart_html"],["live-notices","notice_html"]]) {
      const host = document.getElementById(id);
      if (host && data[key] !== undefined && data[key] !== lastBoard[key] && !host.contains(document.activeElement) && !host.querySelector("details[open]")) {
        host.innerHTML = data[key];
        formatTimes(host);
        lastBoard[key] = data[key];
      }
    }
    const history = document.getElementById("live-history");
    if (history && data.history_html !== lastBoard.history) {
      const openAudio = history.querySelector("details[open] audio");
      if (!openAudio || !data.history_html.includes(openAudio.getAttribute("src"))) {
        history.innerHTML = data.history_html;
        formatTimes(history);
        lastBoard.history = data.history_html;
      }
    }
    document.querySelectorAll("[data-total]").forEach(el => { el.textContent = data[el.dataset.total]; });
    if (data.quota_html && data.quota_html !== previousQuota) {
      document.getElementById("live-quota").innerHTML = data.quota_html;
      previousQuota = data.quota_html;
    }
    if (data.notes_html !== undefined && data.notes_html !== previousNotes) {
      const notes = document.getElementById("agent-staff-notes");
      if (notes) {
        notes.innerHTML = data.notes_html;
        formatTimes(notes);
      }
      previousNotes = data.notes_html;
    }
    if (data.html === previousHTML || liveList.querySelector("form[data-busy]")) return;
    const focused = document.activeElement?.closest("[data-case-id], [data-ticket-id]");
    // The server renders the same escaped Jinja partial as the initial page.
    liveList.innerHTML = data.html;
    previousHTML = data.html;
    formatTimes(liveList);
    filterInbox();
    const selected = liveList.querySelector("[aria-current=page]");
    if (desk.dataset.selected && !selected) {
      const list = new URL("/portal", location.origin);
      list.searchParams.set("tab", desk.dataset.tab);
      if (listStatus) list.searchParams.set("status", listStatus);
      location.replace(list);
      return;
    }
    if (selected?.hasAttribute("data-content-expired") && !desk.hasAttribute("data-content-expired")) {
      location.reload();
      return;
    }
    if (focused?.dataset.caseId) {
      liveList.querySelector('[data-case-id="' + CSS.escape(focused.dataset.caseId) + '"]')?.focus({preventScroll:true});
    } else if (focused?.dataset.ticketId) {
      liveList.querySelector('[data-ticket-id="' + CSS.escape(focused.dataset.ticketId) + '"] button')?.focus({preventScroll:true});
    }
  } catch (error) {
    if (error.status === 401) location.replace("/");
    else if (error.status === 403) location.replace("/portal");
    else syncStatus.textContent = "목록 연결 중…";
  } finally {
    refreshing = false;
  }
}
setInterval(refreshDesk, 4000);
document.addEventListener("visibilitychange", refreshDesk);
window.addEventListener("pageshow", event => { if (event.persisted) refreshDesk(); });

liveList.addEventListener("submit", async event => {
  const form = event.target.closest("form[data-claim]");
  if (!form) return;
  event.preventDefault();
  if (form.dataset.busy) return;
  form.dataset.busy = "1";
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const ticket = await api(form.action, {method:"POST", body:{}});
    location.assign("/portal/" + ticket.case_id);
  } catch (error) {
    delete form.dataset.busy;
    button.disabled = false;
    tell(error.message);
    await refreshDesk();
  }
});

const answer = document.getElementById("chat-message");
document.querySelectorAll(".reply-options button").forEach(button => button.addEventListener("click", () => {
  answer.value = (answer.value ? answer.value + "\n" : "") + button.textContent;
  answer.value = answer.value.slice(0, answer.maxLength);
  button.closest("details").open = false;
  answer.focus();
}));
answer?.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (answer.value.trim()) answer.form.requestSubmit();
  }
});
