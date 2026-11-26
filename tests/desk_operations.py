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
assert title in page(worker, "/portal?tab=dashboard") and page(worker, "/portal?tab=notices").count(title)==1
assert title not in page(guest, "/inquiry")
previous = dict.fromkeys(staff)
team = post(admin, "/admin/teams", dict(name="검증 "+uid(), members=staff[:2], previous=previous), 201)["team_id"]
assert "현재 소속 기준" in page(admin, "/admin?tab=org&team="+team)
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
with app.app_context():
    name=one("SELECT name FROM staff_team WHERE team_id=%s",(team,))["name"]
post(admin, "/admin/teams/"+team, dict(name=name,previous_name=name,previous=previous,members=[]),409)
previous.update({staff[0]:team,staff[1]:team})
post(admin, "/admin/teams/"+team, dict(name=name,previous_name=name,previous=previous,members=[staff[0]]))
assert "기간 내 종결 상담 1건" in page(worker,"/portal?tab=stats&scope=team"+q)
for tab in ("calls","chats","notices","ai","voice-recordings"):
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
