from collections import Counter
from datetime import timedelta
from pathlib import Path

from flask import Blueprint, g, redirect, render_template, request, url_for
from pymysql.err import IntegrityError
from werkzeug.security import generate_password_hash

from .cases import assignment_log, message_info, phone_info, save_phone_response, set_assignee, staff_notes
from .common import ApiError, admin, case_info, case_row, channel, content_visible, payload, staff_user, text_field, uuid_field
from .db import admin_change, db, new_id, one, rows, run, stamp, utcnow
from .desk_reports import channel_metrics, dashboard_charts, midnight, notices, report_range, statistics, today
from .recordings import check_expiry, purge_case_content, stream_recording

management = Blueprint("management", __name__, url_prefix="/admin")
PERIODS = (("daily", "일간"), ("weekly", "주간"), ("monthly", "월간"))


def init_admin():
    """Upgrade existing demo volumes in place and initialize fresh installations."""
    if not one("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() "
               "AND table_name='support_case' AND column_name='deleted_at'"):
        run("ALTER TABLE support_case ADD COLUMN deleted_at DATETIME(6)")
    sql = Path(__file__).with_name("admin_schema.sql").read_text(encoding="utf-8")
    for statement in sql.split(";"):
        if statement.strip():
            run(statement)
    column = one(
        "SELECT IS_NULLABLE AS nullable FROM information_schema.columns WHERE table_schema=DATABASE() "
        "AND table_name='assignment_history' AND column_name='new_agent_id'"
    )
    if column["nullable"] == "NO":
        run("ALTER TABLE assignment_history MODIFY new_agent_id CHAR(36) NULL")
    column = one("SELECT IS_NULLABLE AS nullable FROM information_schema.columns WHERE table_schema=DATABASE() "
                 "AND table_name='customer_session' AND column_name='case_id'")
    if column["nullable"] == "NO":
        run("ALTER TABLE customer_session MODIFY case_id CHAR(36) NULL")
    # AI-only calls have neither a ticket nor a staff uploader.
    if one("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='recording' "
           "AND column_name IN ('case_id','uploaded_by') AND is_nullable='NO' LIMIT 1"):
        run("ALTER TABLE recording MODIFY case_id CHAR(36) NULL, MODIFY uploaded_by CHAR(36) NULL")
    migrate_recording_labels()
    migrate_desk()


def migrate_desk():
    """Apply the additive portal migration, including after a partial DDL run."""
    columns = {(row["tbl"], row["col"]) for row in rows(
        "SELECT table_name AS tbl,column_name AS col FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name IN ('staff_account','support_case')")}
    for chunk in Path(__file__).with_name("desk_schema.sql").read_text(encoding="utf-8").split(";"):
        statement = chunk.strip()
        if not statement:
            continue
        if statement.startswith("ALTER TABLE"):
            tokens = statement.split()
            if (tokens[2], tokens[5]) in columns:
                continue
        run(statement)
    db().commit()


def migrate_recording_labels():
    if not one("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() "
               "AND table_name='recording' AND column_name='label'"):
        run("ALTER TABLE recording ADD COLUMN label VARCHAR(120)")
    if not one("SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() "
               "AND table_name='recording_label'"):
        return

    # Keep the source stable between the copy, byte-for-byte check, and removal.
    run("LOCK TABLES recording WRITE, recording_label WRITE")
    try:
        run("UPDATE recording JOIN recording_label USING (recording_id) "
            "SET recording.label=recording_label.label WHERE recording.label IS NULL")
        if one("SELECT 1 FROM recording_label LEFT JOIN recording USING (recording_id) "
               "WHERE NOT (CAST(recording.label AS BINARY) <=> CAST(recording_label.label AS BINARY)) LIMIT 1"):
            raise RuntimeError("녹취 표시명이 일치하지 않아 기존 recording_label 테이블을 보존했습니다.")
        run("DROP TABLE recording_label")
    except Exception:
        db().rollback()
        raise
    finally:
        run("UNLOCK TABLES")


@management.before_request
def require_admin():
    try:
        g.admin_user = admin()
    except ApiError as error:
        if request.method != "GET":
            raise
        if error.status == 401:
            return redirect(url_for("home"))
        return render_template("denied.html", user=staff_user(), error=error.message), error.status


def quota_rows(staff_id=None):
    # Timestamps in MySQL are UTC. Reporting periods start at Korean midnight.
    kst = timedelta(hours=9)
    today = (utcnow() + kst).replace(hour=0, minute=0, second=0, microsecond=0)
    starts = (today - kst, today - timedelta(days=today.weekday()) - kst, today.replace(day=1) - kst)
    people = rows(
        "SELECT a.staff_id, a.login_id, a.display_name, a.is_active, q.daily, q.weekly, q.monthly FROM staff_account a "
        "LEFT JOIN agent_quota q ON q.staff_id=a.staff_id WHERE a.role='AGENT' "
        + ("AND a.staff_id=%s " if staff_id else "") + "ORDER BY a.is_active DESC, a.login_id",
        (staff_id,) if staff_id else (),
    )
    for table, owner, at, metric in (
        ("assignment_history JOIN visible_case USING (case_id)", "new_agent_id", "changed_at", "assigned"),
        ("visible_case", "current_agent_id", "closed_at", "completed"),
    ):
        counts = ", ".join(f"COUNT(DISTINCT IF({at}>=%s, case_id, NULL)) AS {key}" for key, _ in PERIODS)
        records = rows(
            f"SELECT {owner} AS staff_id, {counts} FROM {table} WHERE {at}>=%s "
            + (f"AND {owner}=%s " if staff_id else "") + f"GROUP BY {owner}",
            (*starts, min(starts), *((staff_id,) if staff_id else ())),
        )
        by_staff = {row["staff_id"]: row for row in records}
        for person in people:
            person[metric] = {key: by_staff.get(person["staff_id"], {}).get(key, 0) for key, _ in PERIODS}
    return people


def agent_account(staff_id, lock=False):
    person = one(
        "SELECT staff_id,login_id,display_name,is_active FROM staff_account WHERE staff_id=%s AND role='AGENT'"
        + (" FOR UPDATE" if lock else ""), (staff_id,),
    )
    if not person:
        raise ApiError("AGENT_NOT_FOUND")
    return person


@management.get("", strict_slashes=False)
def index():
    tab = request.args.get("tab", "overview")
    data = {}
    if tab in ("agents", "accounts"):
        tab = "agents"
        people = quota_rows()
        group = request.args.get("filter", "active")
        data.update(people=[p for p in people if group == "all" or bool(p["is_active"]) == (group != "inactive")], group=group)
        selected = request.args.get("edit")
        data["editor"] = {} if selected == "new" else next((p for p in people if p["staff_id"] == selected), None)
        if selected and selected != "new" and not data["editor"]:
            raise ApiError("AGENT_NOT_FOUND")
    elif tab == "notices":
        data.update(notices=notices(), notice_id=new_id())
    elif tab == "org":
        teams = rows("SELECT team_id,name FROM staff_team ORDER BY name")
        members = rows("SELECT staff_id,login_id,display_name,team_id,is_active FROM staff_account WHERE role='AGENT' ORDER BY login_id")
        selected = request.args.get("team", teams[0]["team_id"] if teams else None)
        team = next((t for t in teams if t["team_id"] == selected), None)
        if selected and not team:
            raise ApiError("INVALID_INPUT")
        dates = report_range()
        data.update(teams=teams, members=members, team=team, dates=dates, snapshot={p["staff_id"]:p["team_id"] for p in members},
                    report=statistics(dates, team_id=selected) if team else None)
    elif tab == "ai":
        data["policy"] = one("SELECT * FROM voice_policy WHERE policy_id=1")
        data["voice_counts"] = one("SELECT COUNT(DISTINCT a.recording_id) AS sessions, COUNT(DISTINCT c.case_id) AS handed "
                                   "FROM audit_event a LEFT JOIN visible_case c ON c.intake_request_id=a.recording_id "
                                   "WHERE a.action IN ('VOICE_RECORDING_AGREED','VOICE_RECORDING_REFUSED') AND a.created_at>=%s", (midnight(today()),))
    elif tab == "quotas":
        data["people"] = quota_rows()
        period = request.args.get("period")
        data["period"] = period if period in dict(PERIODS) else "daily"
        selected = request.args.get("edit")
        if selected:
            return redirect(url_for("management.index", tab="agents", filter="all", edit=selected))
    elif tab == "voice-recordings":
        data["clips"] = rows(
            "SELECT r.recording_id,r.case_id,r.stored_at,r.expires_at,r.label FROM recording r "
            "LEFT JOIN support_case c ON c.case_id=r.case_id "
            "WHERE r.uploaded_by IS NULL AND r.state='READY' AND r.expires_at>UTC_TIMESTAMP(6) AND c.deleted_at IS NULL "
            "ORDER BY r.stored_at DESC,r.recording_id"
        )
    elif tab == "audit":
        page = max(1, min(request.args.get("page", 1, type=int), 2**31 - 1))
        changes = rows(
            "SELECT h.action,h.target,h.case_id,h.created_at,s.login_id,c.deleted_at FROM admin_change h "
            "JOIN staff_account s ON s.staff_id=h.actor_id LEFT JOIN support_case c ON c.case_id=h.case_id "
            "ORDER BY h.change_id DESC LIMIT 41 OFFSET %s",
            ((page - 1) * 40,),
        )
        data.update(changes=changes[:40], page=page, more=len(changes) > 40)
    else:
        tab = tab if tab in ("history", "calls", "chats") else "overview"
        recordings = ""
        if tab == "history":
            recordings = (",(SELECT COUNT(*) FROM recording r WHERE r.case_id=c.case_id) AS recording_count, "
                          "EXISTS(SELECT 1 FROM recording r WHERE r.case_id=c.case_id AND r.expires_at>UTC_TIMESTAMP(6) "
                          "AND r.state NOT IN ('DELETE_PENDING','DESTROYED')) AS editable_recording")
        cases = rows(
            "SELECT c.case_id,c.channel,c.opened_at,c.closed_at,c.content_expires_at,c.current_agent_id,c.inquiry_type,s.login_id,s.display_name" + recordings +
            " FROM visible_case c LEFT JOIN staff_account s ON s.staff_id=c.current_agent_id "
            "ORDER BY c.opened_at DESC,c.case_id"
        )
        items = [dict(case_info(c), login_id=c["login_id"], display_name=c["display_name"], recording_count=c.get("recording_count", 0),
                      content_visible=content_visible(c), editable_recording=c.get("editable_recording")) for c in cases]
        if tab != "history":
            kind = request.args.get("channel")
            if tab == "overview":
                data.update(dashboard_charts())
                data["team"] = quota_rows()
                data["in_progress"] = Counter(i["current_agent_id"] for i in items if i["status"] == "IN_PROGRESS")
            else:
                kind = "PHONE" if tab == "calls" else "WEB"
                data["metrics"] = channel_metrics(kind)
            scoped = [i for i in items if kind not in ("WEB", "PHONE") or i["channel"] == kind]
            counts = Counter(i["status"] for i in scoped)
            status = request.args.get("status", "ALL")
            if status not in ("QUEUED", "IN_PROGRESS", "CLOSED"):
                status = "ALL"
            tickets = [i for i in scoped if status == "ALL" or i["status"] == status]
            page_size = 6
            pages = max(1, (len(tickets) + page_size - 1) // page_size)
            page = max(1, min(request.args.get("page", 1, type=int), pages))
            start = (page - 1) * page_size
            query = {key: request.args[key] for key in ("tab", "channel", "status", "unit", "day", "month") if key in request.args}
            data.update(items=tickets[start:start + page_size], ticket_count=len(tickets), status_filter=status, counts=counts,
                        channel_count=len(scoped), selected=kind, page=page, pages=pages, page_query=query)
        else:
            selected = request.args.get("channel")
            selected = selected if selected in ("WEB", "PHONE", "RECORDINGS") else "ALL"
            data.update(selected=selected, items=[i for i in items if selected == "ALL" or i["channel"] == selected
                                                 or (selected == "RECORDINGS" and i["recording_count"])])
            notes = staff_notes()
            data.update(notes=notes, note_editor=None)
            edit = request.args.get("note")
            if edit == "new":
                data.update(note_editor={}, note_request_id=new_id(),
                            note_tickets=[dict(case_info(c), login_id=c["login_id"]) for c in cases if content_visible(c)])
            elif edit:
                data["note_editor"] = next((n for n in notes if str(n["note_id"]) == edit), None)
                if data["note_editor"] is None:
                    return redirect(url_for("management.index", tab="history", channel=selected))
        g.audit_target = ("CASE_LIST", None, None)
    return render_template("admin.html", tab=tab, user=g.admin_user, periods=PERIODS, **data)


@management.post("/agents", defaults={"staff_id": None})
@management.post("/agents/<uuid:staff_id>")
def save_agent(staff_id):
    person = agent_account(staff_id, lock=True) if staff_id else None
    data = payload()
    login = text_field(data.get("login_id"), 80)
    name = text_field(data["display_name"], 60) if data.get("display_name") else None
    password = data.get("password", "")
    if not isinstance(password, str) or len(password) > 512 or (not person and not password):
        raise ApiError("INVALID_INPUT")
    try:
        if person:
            if "display_name" in data and person["display_name"] != name:
                run("UPDATE staff_account SET display_name=%s WHERE staff_id=%s", (name,staff_id))
                admin_change("상담원 이름 변경", login)
            if login != person["login_id"]:
                run("UPDATE staff_account SET login_id=%s WHERE staff_id=%s", (login, staff_id))
                admin_change("상담원 아이디 변경", person["login_id"] + " → " + login)
            if password:
                run("UPDATE staff_account SET password_hash=%s WHERE staff_id=%s", (generate_password_hash(password), staff_id))
                run("DELETE FROM staff_session WHERE staff_id=%s", (staff_id,))
                admin_change("비밀번호 재설정", login)
        else:
            staff_id = new_id()
            run(
                "INSERT INTO staff_account (staff_id,login_id,display_name,password_hash,role) VALUES (%s,%s,%s,%s,'AGENT')",
                (staff_id, login, name, generate_password_hash(password)),
            )
            admin_change("상담원 계정 생성", login)
    except IntegrityError as error:
        if error.args[0] == 1062:
            raise ApiError("LOGIN_ID_TAKEN") from None
        raise
    if any(key in data for key, _ in PERIODS):
        write_quota(staff_id, login, data)
    return {"staff_id": staff_id}, 200 if person else 201


@management.post("/agents/<uuid:staff_id>/status")
def account_status(staff_id):
    person = agent_account(staff_id, lock=True)
    active = payload().get("status")
    if active not in ("active", "inactive"):
        raise ApiError("INVALID_INPUT")
    enabled = active == "active"
    if enabled != bool(person["is_active"]):
        run("UPDATE staff_account SET is_active=%s WHERE staff_id=%s", (enabled, staff_id))
        if not enabled:
            run("DELETE FROM staff_session WHERE staff_id=%s", (staff_id,))
            opened = rows(
                "SELECT * FROM visible_case WHERE current_agent_id=%s AND closed_at IS NULL ORDER BY case_id FOR UPDATE",
                (staff_id,),
            )
            for case in opened:
                set_assignee(case, None, g.actor)
                admin_change("상담 배정 해제", person["login_id"], case["case_id"])
        admin_change("계정 활성화" if enabled else "계정 비활성화", person["login_id"])
    return {"active": enabled}


@management.post("/agents/<uuid:staff_id>/quota")
def save_quota(staff_id):
    person = agent_account(staff_id, lock=True)
    write_quota(staff_id, person["login_id"], payload())
    return {"staff_id": staff_id}


def write_quota(staff_id, login, data):
    goals = []
    for key, _ in PERIODS:
        if key not in data:
            raise ApiError("INVALID_INPUT")
        value = data[key]
        if value is None or value == "":
            goals.append(None)
        elif str(value).isascii() and str(value).isdecimal() and len(str(value)) <= 5:
            goals.append(int(value))
        else:
            raise ApiError("INVALID_INPUT")
    previous = one("SELECT daily,weekly,monthly FROM agent_quota WHERE staff_id=%s", (staff_id,)) or {}
    if goals != [previous.get(key) for key, _ in PERIODS]:
        run("REPLACE INTO agent_quota (staff_id,daily,weekly,monthly) VALUES (%s,%s,%s,%s)", (staff_id, *goals))
        summary = " / ".join("미설정" if goal is None else str(goal) for goal in goals)
        admin_change("할당량 변경", f"{login} · 일/주/월 {summary}")


@management.post("/notices")
def publish_notice():
    data = payload()
    notice_id = uuid_field(data.get("notice_id"))
    title, body = text_field(data.get("title"), 100), text_field(data.get("body"), 3000)
    try:
        run("INSERT INTO staff_notice (notice_id,author_id,title,body) VALUES (%s,%s,%s,%s)", (notice_id,g.actor,title,body))
    except IntegrityError as error:
        if error.args[0] != 1062:
            raise
        old = one("SELECT author_id,title,body FROM staff_notice WHERE notice_id=%s", (notice_id,))
        if old != dict(author_id=g.actor,title=title,body=body):
            raise ApiError("REQUEST_KEY_CONFLICT") from None
        return {"notice_id": notice_id}
    admin_change("공지 게시", title)
    return {"notice_id": notice_id}, 201


@management.post("/teams", defaults={"team_id": None})
@management.post("/teams/<uuid:team_id>")
def save_team(team_id):
    data = payload()
    name = text_field(data.get("name"), 40)
    members, previous = data.get("members"), data.get("previous")
    if not isinstance(members, list) or not isinstance(previous, dict):
        raise ApiError("INVALID_INPUT")
    chosen = {uuid_field(value) for value in members}
    new = team_id is None
    team_id = team_id or new_id()
    if not new:
        team = one("SELECT name FROM staff_team WHERE team_id=%s FOR UPDATE", (team_id,))
        if not team or team["name"] != data.get("previous_name"):
            raise ApiError("TEAM_CHANGED")
    try:
        if new:
            run("INSERT INTO staff_team (team_id,name) VALUES (%s,%s)", (team_id,name))
        else:
            run("UPDATE staff_team SET name=%s WHERE team_id=%s", (name,team_id))
    except IntegrityError as error:
        if error.args[0] == 1062:
            raise ApiError("TEAM_NAME_TAKEN") from None
        raise
    current = {a["staff_id"]: a["team_id"] for a in rows(
        "SELECT staff_id,team_id FROM staff_account WHERE role='AGENT' ORDER BY staff_id FOR UPDATE")}
    original = {key for key, value in previous.items() if value == team_id}
    if not chosen.issubset(current) or {key for key, value in current.items() if value == team_id} != original:
        raise ApiError("TEAM_CHANGED")
    for key in sorted(original ^ chosen):
        if key not in previous or previous[key] != current[key]:
            raise ApiError("TEAM_CHANGED")
        run("UPDATE staff_account SET team_id=%s WHERE staff_id=%s", (team_id if key in chosen else None,key))
    admin_change("팀 생성" if new else "팀 구성 수정", name)
    return {"team_id": team_id}, 201 if new else 200


@management.post("/ai-settings")
def save_ai_policy():
    data = payload()
    enabled, offer, instructions = data.get("enabled"), data.get("offer_handoff"), data.get("instructions")
    if not isinstance(enabled, bool) or not isinstance(offer, bool) or not isinstance(instructions, str) or len(instructions) > 600:
        raise ApiError("INVALID_INPUT")
    run("UPDATE voice_policy SET enabled=%s,offer_handoff=%s,instructions=%s WHERE policy_id=1", (enabled,offer,instructions.strip()))
    admin_change("AI 응대 설정 변경", "AI 음성 상담")
    return {"enabled": enabled}


@management.get("/cases/<uuid:case_id>")
def history(case_id):
    case = case_row(case_id)
    g.audit_target = ("CASE_VIEW", case_id, None)
    visible = content_visible(case)
    data = {}
    if case["channel"] == "WEB":
        messages = rows("SELECT * FROM chat_message WHERE case_id=%s AND body<>'' ORDER BY message_id", (case_id,)) if visible else []
        data["messages"] = [message_info(m) for m in messages]
    else:
        data["phone"] = phone_info(case_id) if visible else None
        data["recordings"] = rows(
            "SELECT r.recording_id,r.state,r.stored_at,r.expires_at,r.size_bytes,r.uploaded_by IS NULL AS ai,"
            "(r.expires_at>UTC_TIMESTAMP(6) AND r.state NOT IN ('DELETE_PENDING','DESTROYED')) AS editable,"
            "CASE WHEN r.expires_at>UTC_TIMESTAMP(6) AND r.state NOT IN ('DELETE_PENDING','DESTROYED') THEN r.label END AS label "
            "FROM recording r "
            "WHERE r.case_id=%s ORDER BY r.expires_at DESC,r.recording_id", (case_id,),
        )
    return render_template("admin.html", tab="history", user=g.admin_user, detail=case_info(case), assignments=assignment_log(case_id),
                           visible=visible, notes=staff_notes(case_id) if visible else [], result=case["result_note"] if visible else None, **data)


def editable_history(case_id, expected=None):
    case = case_row(case_id, lock=True)
    if expected:
        channel(case, expected)
    if not content_visible(case):
        raise ApiError("CONTENT_EXPIRED")
    return case


@management.post("/notes")
def create_note():
    data = payload()
    case_id = uuid_field(data.get("case_id"))
    request_id = uuid_field(data.get("request_id"))
    body = text_field(data.get("body"))
    editable_history(case_id)
    existing = one("SELECT note_id,created_by,body FROM staff_note WHERE case_id=%s AND request_id=%s", (case_id, request_id))
    if existing:
        if existing["created_by"] != g.actor or existing["body"] != body:
            raise ApiError("REQUEST_KEY_CONFLICT")
        return {"note_id": existing["note_id"]}
    note_id = run("INSERT INTO staff_note (case_id,request_id,created_by,body) VALUES (%s,%s,%s,%s)",
                  (case_id, request_id, g.actor, body))
    admin_change("직원용 기록 추가", f"기록 #{note_id}", case_id)
    return {"note_id": note_id}, 201


@management.post("/cases/<uuid:case_id>/notes/<int:note_id>")
def change_note(case_id, note_id):
    editable_history(case_id)
    data = payload()
    note = one("SELECT body,updated_at FROM staff_note WHERE case_id=%s AND note_id=%s", (case_id, note_id))
    delete = data.get("operation") == "delete"
    if not note or (note["body"] is None and not delete):
        raise ApiError("STAFF_NOTE_NOT_FOUND")
    body = None if delete else text_field(data.get("body"))
    if note["body"] != body:
        if data.get("updated_at") != stamp(note["updated_at"]):
            raise ApiError("STAFF_NOTE_CHANGED")
        # Clearing the body keeps a retried creation from resurrecting a deleted note.
        run("UPDATE staff_note SET body=%s WHERE case_id=%s AND note_id=%s", (body, case_id, note_id))
        admin_change("직원용 기록 삭제" if delete else "직원용 기록 수정", f"기록 #{note_id}", case_id)
    return {"note_id": note_id}


@management.post("/cases/<uuid:case_id>/messages/<int:message_id>")
def edit_message(case_id, message_id):
    editable_history(case_id, "WEB")
    message = one("SELECT body FROM chat_message WHERE case_id=%s AND message_id=%s AND body<>''", (case_id, message_id))
    if not message:
        raise ApiError("MESSAGE_NOT_FOUND")
    data = payload()
    if data.get("operation") == "delete":
        # Keep the send request ID until retention cleanup so a delayed retry cannot restore deleted text.
        run("UPDATE chat_message SET body='' WHERE case_id=%s AND message_id=%s", (case_id, message_id))
        admin_change("메시지 삭제", f"메시지 #{message_id}", case_id)
    else:
        body = text_field(data.get("body"))
        if body != message["body"]:
            run("UPDATE chat_message SET body=%s WHERE case_id=%s AND message_id=%s", (body, case_id, message_id))
            admin_change("메시지 수정", f"메시지 #{message_id}", case_id)
    return {"message_id": str(message_id)}


@management.post("/cases/<uuid:case_id>/history")
def edit_history(case_id):
    data = payload()
    if data.get("operation") == "delete_ticket":
        return delete_ticket(case_id)
    case = editable_history(case_id)
    if data.get("operation") == "delete":
        if case["channel"] == "WEB":
            run("UPDATE chat_message SET body='' WHERE case_id=%s AND body<>''", (case_id,))
        else:
            run("UPDATE phone_response SET call_at=NULL, consent_state='UNASKED', consent_by=NULL, consent_at=NULL, "
                "inquiry=NULL, guidance=NULL, followup_required=NULL WHERE case_id=%s", (case_id,))
            run("UPDATE recording SET state='DELETE_PENDING' WHERE case_id=%s AND state IN ('PENDING','FAILED','READY')", (case_id,))
        run("UPDATE staff_note SET body=NULL WHERE case_id=%s AND body IS NOT NULL", (case_id,))
        run("UPDATE support_case SET result_note=NULL WHERE case_id=%s", (case_id,))
        admin_change("상담 기록 삭제", "웹채팅" if case["channel"] == "WEB" else "전화 응대 및 녹취", case_id)
    else:
        fields = {key: data[key] for key in ("call_at", "inquiry", "guidance", "followup_required") if key in data}
        if fields:
            channel(case, "PHONE")
            save_phone_response(case_id, fields, allow_clear=True)
        if "result_note" in data and case["closed_at"]:
            run("UPDATE support_case SET result_note=%s WHERE case_id=%s", (text_field(data["result_note"]), case_id))
        elif not fields:
            raise ApiError("INVALID_INPUT")
        admin_change("상담 기록 수정", "웹채팅" if case["channel"] == "WEB" else "전화 응대", case_id)
    return {"case_id": case_id}


def delete_ticket(case_id):
    case = one("SELECT channel,deleted_at FROM support_case WHERE case_id=%s FOR UPDATE", (case_id,))
    if not case:
        raise ApiError("CASE_NOT_FOUND")
    if case["deleted_at"] is None:
        purge_case_content(case_id)
        run("DELETE FROM entry_code WHERE case_id=%s", (case_id,))
        run("DELETE FROM customer_session WHERE case_id=%s", (case_id,))
        run("UPDATE recording SET state='DELETE_PENDING' WHERE case_id=%s AND state IN ('PENDING','FAILED','READY')", (case_id,))
        # Keep the case and intake IDs for the audit trail and late intake retries.
        run("UPDATE support_case SET deleted_at=UTC_TIMESTAMP(6),support_subject_ref=NULL,current_agent_id=NULL "
            "WHERE case_id=%s", (case_id,))
        admin_change("상담 티켓 삭제", "웹채팅" if case["channel"] == "WEB" else "전화", case_id)
    return "", 204


@management.get("/voice-recordings/<uuid:recording_id>/audio")
def play_voice(recording_id):
    item = one("SELECT * FROM recording WHERE recording_id=%s AND uploaded_by IS NULL FOR UPDATE", (recording_id,))
    g.audit_target = ("RECORDING_PLAY", item["case_id"] if item else None, recording_id)
    if not item:
        raise ApiError("RECORDING_NOT_FOUND")
    return stream_recording(item)


@management.post("/voice-recordings/<uuid:recording_id>", defaults={"case_id": None})
@management.post("/cases/<uuid:case_id>/recordings/<uuid:recording_id>")
def edit_recording(case_id, recording_id):
    if case_id:
        channel(case_row(case_id, lock=True), "PHONE")
        item = one("SELECT state,expires_at,case_id FROM recording WHERE case_id=%s AND recording_id=%s FOR UPDATE",
                   (case_id, recording_id))
    else:
        item = one("SELECT state,expires_at,case_id FROM recording WHERE uploaded_by IS NULL AND recording_id=%s FOR UPDATE",
                   (recording_id,))
    if not item:
        raise ApiError("RECORDING_NOT_FOUND")
    case_id = item["case_id"]
    data = payload()
    if data.get("operation") == "delete":
        if item["state"] not in ("DELETE_PENDING", "DESTROYED"):
            run("UPDATE recording SET state='DELETE_PENDING' WHERE recording_id=%s", (recording_id,))
            admin_change("녹취 삭제 요청", recording_id, case_id)
    else:
        check_expiry(item)
        label = data.get("label")
        if not isinstance(label, str) or len(label) > 120:
            raise ApiError("INVALID_INPUT")
        run("UPDATE recording SET label=%s WHERE recording_id=%s", (label.strip(), recording_id))
        admin_change("녹취 표시명 수정", recording_id, case_id)
    return {"recording_id": recording_id}
