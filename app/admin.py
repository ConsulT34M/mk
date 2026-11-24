from collections import Counter
from datetime import timedelta
from pathlib import Path

from flask import Blueprint, g, redirect, render_template, request, url_for
from pymysql.err import IntegrityError
from werkzeug.security import generate_password_hash

from .cases import assignment_log, message_info, phone_info, save_phone_response, set_assignee, staff_notes
from .common import ApiError, admin, case_info, case_row, channel, content_visible, payload, staff_user, text_field, uuid_field
from .db import admin_change, new_id, one, rows, run, stamp, utcnow
from .recordings import check_expiry, purge_case_content

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
        "SELECT a.staff_id, a.login_id, a.is_active, q.daily, q.weekly, q.monthly FROM staff_account a "
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
        "SELECT staff_id,login_id,is_active FROM staff_account WHERE staff_id=%s AND role='AGENT'"
        + (" FOR UPDATE" if lock else ""), (staff_id,),
    )
    if not person:
        raise ApiError("AGENT_NOT_FOUND")
    return person


@management.get("", strict_slashes=False)
def index():
    tab = request.args.get("tab", "overview")
    data = {}
    if tab == "agents":
        data["people"] = rows("SELECT staff_id,login_id,is_active FROM staff_account WHERE role='AGENT' ORDER BY is_active DESC,login_id")
        selected = request.args.get("edit")
        data["editor"] = {} if selected == "new" else agent_account(selected) if selected else None
    elif tab == "quotas":
        data["people"] = quota_rows()
        period = request.args.get("period")
        data["period"] = period if period in dict(PERIODS) else "daily"
        selected = request.args.get("edit")
        data["editor"] = next((p for p in data["people"] if p["staff_id"] == selected), None)
        if selected and not data["editor"]:
            raise ApiError("AGENT_NOT_FOUND")
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
        tab = "history" if tab == "history" else "overview"
        recordings = ""
        if tab == "history":
            recordings = (",(SELECT COUNT(*) FROM recording r WHERE r.case_id=c.case_id) AS recording_count, "
                          "EXISTS(SELECT 1 FROM recording r WHERE r.case_id=c.case_id AND r.expires_at>UTC_TIMESTAMP(6) "
                          "AND r.state NOT IN ('DELETE_PENDING','DESTROYED')) AS editable_recording")
        cases = rows(
            "SELECT c.case_id,c.channel,c.opened_at,c.closed_at,c.content_expires_at,c.current_agent_id,s.login_id" + recordings +
            " FROM visible_case c LEFT JOIN staff_account s ON s.staff_id=c.current_agent_id "
            "ORDER BY c.opened_at DESC,c.case_id"
        )
        items = [dict(case_info(c), login_id=c["login_id"], recording_count=c.get("recording_count", 0),
                      content_visible=content_visible(c), editable_recording=c.get("editable_recording")) for c in cases]
        if tab == "overview":
            selected = request.args.get("status", "ALL")
            counts = {key: sum(i["status"] == key for i in items) for key in ("QUEUED", "IN_PROGRESS", "CLOSED")}
            selected = selected if selected in counts else "ALL"
            data.update(counts=counts, total=len(items), selected=selected)
            data["team"] = quota_rows()
            data["in_progress"] = Counter(i["current_agent_id"] for i in items if i["status"] == "IN_PROGRESS")
            data["items"] = [i for i in items if selected == "ALL" or i["status"] == selected]
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
    password = data.get("password", "")
    if not isinstance(password, str) or len(password) > 512 or (not person and not password):
        raise ApiError("INVALID_INPUT")
    try:
        if person:
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
                "INSERT INTO staff_account (staff_id,login_id,password_hash,role) VALUES (%s,%s,%s,'AGENT')",
                (staff_id, login, generate_password_hash(password)),
            )
            admin_change("상담원 계정 생성", login)
    except IntegrityError as error:
        if error.args[0] == 1062:
            raise ApiError("LOGIN_ID_TAKEN") from None
        raise
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
    data = payload()
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
        admin_change("할당량 변경", f"{person['login_id']} · 일/주/월 {summary}")
    return {"staff_id": staff_id}


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
            "SELECT r.recording_id,r.state,r.stored_at,r.expires_at,r.size_bytes,"
            "(r.expires_at>UTC_TIMESTAMP(6) AND r.state NOT IN ('DELETE_PENDING','DESTROYED')) AS editable,"
            "CASE WHEN r.expires_at>UTC_TIMESTAMP(6) AND r.state NOT IN ('DELETE_PENDING','DESTROYED') THEN l.label END AS label "
            "FROM recording r LEFT JOIN recording_label l ON l.recording_id=r.recording_id "
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


@management.post("/cases/<uuid:case_id>/recordings/<uuid:recording_id>")
def edit_recording(case_id, recording_id):
    channel(case_row(case_id, lock=True), "PHONE")
    item = one("SELECT state,expires_at FROM recording WHERE case_id=%s AND recording_id=%s FOR UPDATE", (case_id, recording_id))
    if not item:
        raise ApiError("RECORDING_NOT_FOUND")
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
        run("REPLACE INTO recording_label (recording_id,label) VALUES (%s,%s)", (recording_id, label.strip()))
        admin_change("녹취 표시명 수정", recording_id, case_id)
    return {"recording_id": recording_id}
