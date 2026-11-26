"""Run inside a disposable MySQL-backed app. All staff and cases below are synthetic."""
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app import app
from app.admin import migrate_desk
from app.db import db, one, run
from app.desk_reports import case_status_breakdown, dashboard_charts, midnight, today


def uid():
    return str(uuid4())


admin, worker, peer, outside, guest = [app.test_client() for _ in range(5)]


def post(client, path, values, expected=200):
    response = client.post(path, json=values)
    assert response.status_code == expected, (path, response.status_code, response.json)
    return response.json


def page(client, path):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.get_data(as_text=True)


def dashboard_counts():
    with app.app_context():
        return {part["label"]: part["count"] for part in case_status_breakdown()["slices"]}


post(admin, "/staff/sessions", {"login_id":"admin","password":os.environ["DEMO_ADMIN_PASSWORD"]})
staff = []
for client in (worker, peer, outside):
    login, password = uid(), uid()
    account = post(admin, "/admin/agents", dict(login_id=login,password=password,display_name="검증 상담원",daily=4,weekly=20,monthly=80), 201)
    post(client, "/staff/sessions", dict(login_id=login,password=password))
    staff.append(account["staff_id"])
assert "일간 목표" in page(admin, "/admin?tab=agents") and "계정 삭제" in page(admin,"/admin?tab=agents&edit="+staff[0])
title = "교대 안내 " + uid()
notice = dict(notice_id=uid(),title=title,body="첫 줄\n다음 줄")
post(worker, "/admin/notices", notice, 403)
post(guest, "/admin/notices", notice, 401)
post(admin, "/admin/notices", notice, 201)
post(admin, "/admin/notices", notice)
assert title in page(worker, "/portal?tab=dashboard") and page(worker, "/portal?tab=board").count(title)==1
assert title not in page(guest, "/inquiry")
with app.app_context():
    default_team = one("SELECT team_id FROM staff_team WHERE name='고객 상담팀' AND is_default=TRUE")["team_id"]
    assert one("SELECT COUNT(*) AS n FROM staff_account WHERE staff_id IN (%s,%s,%s) AND team_id=%s",
               (*staff, default_team))["n"] == len(staff)
previous = dict.fromkeys(staff, default_team)
team_name = "검증 " + uid()
team = post(admin, "/admin/teams", dict(name=team_name, members=staff[:2], previous=previous), 201)["team_id"]
org = page(admin, "/admin?tab=org&team="+team)
assert "현재 소속 기준" in org and 'aria-label="팀 조직도"' in org

def team_dashboard(client):
    html = client.get("/portal/updates?tab=dashboard&team=" + team).json["quota_html"]
    return html.split('aria-label="팀 배정량"', 1)[1]

assert "0 / 8건" in team_dashboard(worker) and team_name not in team_dashboard(outside)
post(admin, "/admin/agents/"+staff[0]+"/quota", dict(daily=5,weekly=20,monthly=80))
assert "0 / 9건" in team_dashboard(peer)
post(admin, "/admin/agents/"+staff[0]+"/quota", dict(daily=4,weekly=20,monthly=80))
team_edit = dict(name=team_name, previous_name=team_name, members=staff[:2],
                 previous={**previous,staff[0]:team,staff[1]:team}, previous_goals=[None]*3,
                 daily=0,weekly="",monthly=300)
team_path = "/admin/teams/" + team
post(worker, team_path, team_edit, 403)
post(admin, team_path, {**team_edit, "monthly": -1}, 400)
post(admin, team_path, team_edit)
team_html = team_dashboard(worker)
assert all(f"0 / {goal}건" in team_html for goal in (0,40,300))
post(admin, team_path, {**team_edit, "daily": 7}, 409)
post(admin, team_path, {**team_edit, "daily":"", "monthly":"", "previous_goals":[0,None,300]})
assert "0 / 160건" in team_dashboard(worker)
counts = dashboard_counts()
for client, channel in [(worker,"WEB"),(peer,"PHONE"),(outside,"WEB")]:
    ticket = admin.post("/cases", json=dict(support_subject_ref=uid(),intake_request_id=uid(),channel=channel,inquiry_type="TRADE"),
                        headers={"Authorization":"Bearer "+os.environ["PARTNER_TOKEN"]})
    assert ticket.status_code == 201
    assert dashboard_counts() == counts | {"대기": counts["대기"] + 1}
    case_id = ticket.json["case_id"]
    path = "/staff/cases/"+case_id
    post(client, path+"/claim", {})
    assert dashboard_counts() == counts | {"진행 중": counts["진행 중"] + 1}
    post(client, path+"/category", {"inquiry_type":"FUNDS"})
    channel_tab = "calls" if channel == "PHONE" else "chats"
    list_path = "/portal/updates?tab=" + channel_tab
    assert case_id in client.get(list_path + "&status=active").json["html"]
    assert case_id not in client.get("/portal/updates?tab=closed").json["html"]
    if channel=="PHONE":
        assert client.get(path).json["phone_response"]["call_at"] is None
        post(client, path+"/phone-response", dict(call_at=datetime.now(timezone.utc).isoformat(),inquiry="검증",guidance="안내",
             followup_required=False,consent_state="REFUSED",talk_seconds=75,call_direction="OUTBOUND"))
    post(client, path+"/close", dict(result_note="종결",outcome="RESOLVED"))
    counts["종결"] += 1
    assert dashboard_counts() == counts
    for suffix in ("", "&status=all", "&status=active", "&status=closed"):
        active = client.get(list_path + suffix).json
        assert case_id not in active["html"] and not active["history_html"]
    assert case_id not in page(client, "/portal?tab=" + channel_tab + "&status=all")
    archived = client.get("/portal/updates?tab=closed").json
    assert case_id in archived["html"] and case_id in archived["history_html"]
    for selected_channel in ("PHONE", "WEB"):
        filtered = client.get("/portal/updates?tab=closed&channel=" + selected_channel).json
        assert (case_id in filtered["html"]) == (selected_channel == channel)
        assert (case_id in filtered["history_html"]) == (selected_channel == channel)
    closed_detail = page(client, "/portal/" + case_id + "?tab=" + channel_tab + "&status=closed")
    assert 'data-tab="closed"' in closed_detail and f'data-channel="{channel}"' in closed_detail
    if channel=="PHONE":
        phone_id=case_id
chart_url = "/portal/updates?tab=dashboard"
chart_html = worker.get(chart_url).json["chart_html"].strip()
assert chart_html == outside.get(chart_url).json["chart_html"].strip()
assert chart_html in page(worker, "/portal?tab=dashboard")
assert f">{sum(counts.values())}건</text>" in chart_html
local_day=today().isoformat()
q="&start="+local_day+"&end="+local_day
assert "기간 내 종결 상담 1건" in page(worker,"/portal?tab=stats"+q)
assert "기간 내 종결 상담 2건" in page(worker,"/portal?tab=stats&scope=team"+q)
assert "기간 내 종결 상담 1건" in page(outside,"/portal?tab=stats&scope=team&team="+team+q)
assert "1분 15초" in page(worker,"/portal?tab=stats&scope=team&by=duration"+q)
assert "입출금" in page(worker,"/portal?tab=stats&scope=team&by=topics"+q)
assert "해결" in page(worker,"/portal?tab=stats&scope=team&by=outcomes"+q)
assert worker.get("/portal?tab=stats&start=2025-03-01&end=2025-01-01").status_code==400
with app.app_context():
    run("UPDATE phone_response SET call_at=NULL WHERE case_id=%s",(phone_id,))
    db().commit()
assert phone_id in page(peer,"/portal?tab=recordings"+q)
assert phone_id not in page(worker,"/portal?tab=recordings"+q)
assert phone_id not in worker.get("/portal/updates?tab=closed").json["html"]
assert phone_id not in worker.get("/portal/updates?tab=closed").json["history_html"]
assert "2 / 8건" in team_dashboard(worker)
transfer = admin.post("/cases", headers={"Authorization":"Bearer "+os.environ["PARTNER_TOKEN"]},
                      json=dict(support_subject_ref=uid(),intake_request_id=uid(),channel="WEB")).json["case_id"]
post(worker, "/staff/cases/"+transfer+"/claim", {})
post(admin, "/staff/cases/"+transfer+"/assignee", {"new_agent_id":staff[1],"previous_agent_id":staff[0]})
assert "배정 3건" in team_dashboard(worker) and "배정 4건" not in team_dashboard(worker)
with app.app_context():
    name=one("SELECT name FROM staff_team WHERE team_id=%s",(team,))["name"]
post(admin, "/admin/teams/"+team, dict(name=name,previous_name=name,previous=previous,members=[]),409)
previous.update({staff[0]:team,staff[1]:team})
post(admin, "/admin/teams/"+team, dict(name=name,previous_name=name,previous=previous,members=[staff[0]]))
assert "기간 내 종결 상담 1건" in page(worker,"/portal?tab=stats&scope=team"+q)
assert "1 / 4건" in team_dashboard(worker)
unassigned = peer.get("/portal/updates?tab=dashboard").json["quota_html"]
assert 'aria-label="내 배정량"' in unassigned and 'aria-label="팀 배정량"' not in unassigned
for tab in ("calls","chats","board","ai","voice-recordings"):
    page(admin,"/admin?tab="+tab)
for tab in ("calls","chats","closed"):
    page(worker,"/portal?tab="+tab)
def daily_guide(day):
    with app.test_request_context("/admin?unit=day&day=" + day.isoformat()):
        return next(part["count"] for part in dashboard_charts()["topics"]["slices"] if part["label"] == "이용 안내")


current_day = today()
prior_day = current_day - timedelta(days=1)
before = [daily_guide(day) for day in (prior_day, current_day)]
with app.app_context():
    migrate_desk()
    assert one("SELECT team_id FROM staff_account WHERE staff_id=%s", (staff[0],))["team_id"] == team
    assert one("SELECT team_id FROM staff_account WHERE staff_id=%s", (staff[1],))["team_id"] is None
    assert one("SELECT COUNT(*) AS n FROM staff_notice WHERE notice_id=%s", (notice["notice_id"],))["n"] == 1
    for offset in (0, 1):
        instant = midnight(current_day) - timedelta(seconds=offset)
        run("INSERT INTO support_case(case_id,channel,opened_at,inquiry_type) VALUES (%s,'WEB',%s,'GUIDE')", (uid(), instant))
    db().commit()
assert [daily_guide(day) for day in (prior_day, current_day)] == [count + 1 for count in before]
with app.test_request_context("/admin?unit=day&day=" + local_day):
    all_channels, phone, web = (dashboard_charts(kind) for kind in (None, "PHONE", "WEB"))
    assert all_channels["topics"]["total"] == phone["topics"]["total"] + web["topics"]["total"]
    assert all(slot["web"] == 0 for slot in phone["slots"]) and all(slot["phone"] == 0 for slot in web["slots"])
assert "시간대별 접수" in page(admin,"/admin?unit=day&day="+local_day)
assert local_day[:7] in page(admin,"/admin?unit=month&month="+local_day[:7])
policy=dict(enabled=False,instructions="운영 테스트",offer_handoff=False)
post(worker,"/admin/ai-settings",policy,403)
post(admin,"/admin/ai-settings",policy)
assert "운영이 중지" in page(guest,"/inquiry")
post(guest,"/inquiry/voice/sessions",{},503)
post(admin,"/admin/ai-settings",dict(enabled=True,instructions="",offer_handoff=True))
print("PASS portal dashboards, notice publishing, team transfers and scope, date reports, actual talk time, recording ownership, voice controls and repeatable migration")
