import hmac
import os
import secrets
from datetime import datetime, timezone

from flask import Blueprint, g, redirect, request, url_for

from .common import (
    CHAT_COOKIE, INQUIRY_COOKIE, ApiError, admin, agent, assigned_case, case_info, case_row, channel,
    content_visible, customer_session, deadline, digest, payload, staff_user, status,
    text_field, uuid_field, writable,
)
from .db import admin_change, new_id, one, rows, run, stamp, utcnow
from .desk_reports import DIRECTIONS, OUTCOMES, TOPICS
from .recordings import recording_info

cases = Blueprint("cases", __name__)


def partner():
    provided = request.headers.get("Authorization", "").encode()
    expected = ("Bearer " + os.environ["PARTNER_TOKEN"]).encode()
    if not hmac.compare_digest(provided, expected):
        raise ApiError("CLIENT_AUTH_REQUIRED")
    g.actor = "exchange"


def intake_channel(value):
    if value not in ("WEB", "PHONE"):
        raise ApiError("INVALID_INPUT")
    return value


@cases.route("/cases", methods=["GET", "POST"])
def exchange_cases():
    partner()
    if request.method == "GET":
        subject = uuid_field(request.args.get("support_subject_ref"))
        kind = intake_channel(request.args.get("channel", "WEB"))
        items = rows(
            "SELECT * FROM visible_case WHERE support_subject_ref=%s AND channel=%s ORDER BY opened_at DESC,case_id",
            (subject, kind),
        )
        return {"items": [{"case_id": case["case_id"], "status": status(case)} for case in items]}

    data = payload()
    if not data.get("intake_request_id"):
        raise ApiError("MISSING_INTAKE_REQUEST_ID")
    subject = uuid_field(data.get("support_subject_ref"))
    intake = uuid_field(data["intake_request_id"])
    kind = intake_channel(data.get("channel", "WEB"))
    case_id, created = intake_case(subject, intake, kind, data.get("inquiry_type"))
    return {"case_id": case_id}, 201 if created else 200


def intake_case(subject, intake, kind, inquiry_type=None):
    if inquiry_type is not None and (not isinstance(inquiry_type, str) or inquiry_type not in TOPICS):
        raise ApiError("INVALID_INPUT")
    candidate = new_id()
    run(
        "INSERT INTO support_case (case_id, channel, support_subject_ref, intake_request_id,inquiry_type) "
        "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE case_id=case_id",
        (candidate, kind, subject, intake, inquiry_type),
    )
    case = one("SELECT case_id,channel,support_subject_ref,deleted_at FROM support_case WHERE intake_request_id=%s", (intake,))
    if case["deleted_at"]:
        raise ApiError("TICKET_DELETED")
    if case["support_subject_ref"] != subject or case["channel"] != kind:
        raise ApiError("REQUEST_KEY_CONFLICT")
    created = case["case_id"] == candidate
    if created and kind == "PHONE":
        run("INSERT INTO phone_response (case_id) VALUES (%s)", (candidate,))
    return case["case_id"], created


@cases.post("/cases/<uuid:case_id>/entry-codes")
def grant_entry(case_id):
    partner()
    subject = uuid_field(payload().get("support_subject_ref"))
    case = case_row(case_id, lock=True)
    channel(case, "WEB")
    if case["support_subject_ref"] != subject:
        raise ApiError("CASE_NOT_FOUND")
    code = secrets.token_urlsafe(32)
    expires = deadline("ENTRY_CODE_MINUTES", 5)
    run("INSERT INTO entry_code (code_hash, case_id, expires_at) VALUES (%s,%s,%s)", (digest(code), case_id, expires))
    return {"case_id": case_id, "entry_code": code, "expires_at": stamp(expires)}, 201


@cases.get("/staff/cases")
def staff_cases():
    user = staff_user()
    g.audit_target = ("CASE_LIST", None, None)
    if user["role"] == "ADMIN":
        result = rows("SELECT * FROM visible_case ORDER BY opened_at DESC")
    else:
        result = rows("SELECT * FROM visible_case WHERE current_agent_id=%s ORDER BY opened_at DESC", (user["staff_id"],))
    return {"items": [case_info(case) for case in result]}


@cases.get("/staff/agents")
def agents():
    admin()
    return {"items": rows("SELECT staff_id, login_id FROM staff_account WHERE role='AGENT' AND is_active=1 ORDER BY login_id")}


def waiting_tickets():
    items = rows(
        "SELECT c.case_id,c.channel,c.inquiry_type,c.opened_at, COALESCE((SELECT MAX(a.changed_at) FROM assignment_history a "
        "WHERE a.case_id=c.case_id AND a.new_agent_id IS NULL),c.opened_at) AS queued_at FROM visible_case c "
        "WHERE c.current_agent_id IS NULL AND c.closed_at IS NULL ORDER BY queued_at,c.case_id"
    )
    for item in items:
        item["wait_seconds"] = int((utcnow() - item.pop("queued_at")).total_seconds())
        item["opened_at"] = stamp(item["opened_at"])
    return items


def agent_inbox(staff_id):
    items = rows(
        "SELECT c.*, CASE WHEN c.channel='PHONE' THEN LEFT(p.inquiry,100) ELSE "
        "(SELECT LEFT(m.body,100) FROM chat_message m WHERE m.case_id=c.case_id AND m.body<>'' "
        "ORDER BY m.message_id DESC LIMIT 1) END AS preview "
        "FROM visible_case c LEFT JOIN phone_response p ON p.case_id=c.case_id "
        "WHERE c.current_agent_id=%s ORDER BY c.opened_at DESC, c.case_id", (staff_id,),
    )
    result = []
    for item in items:
        expired = not content_visible(item)
        result.append(dict(case_info(item), preview=None if expired else item["preview"], content_expired=expired))
    return result


def recent_closed_cases(staff_id):
    items = rows(
        "SELECT c.*, (SELECT r.recording_id FROM recording r WHERE r.case_id=c.case_id AND r.state='READY' "
        "AND r.expires_at>UTC_TIMESTAMP(6) ORDER BY r.stored_at DESC,r.recording_id LIMIT 1) AS clip_id "
        "FROM visible_case c WHERE c.current_agent_id=%s AND c.closed_at IS NOT NULL "
        "ORDER BY c.closed_at DESC,c.case_id LIMIT 6", (staff_id,),
    )
    return [dict(case_info(item), clip_id=item["clip_id"]) for item in items]


@cases.post("/staff/cases/<uuid:case_id>/assignee")
def assign(case_id):
    user = admin()
    data = payload()
    new_agent = uuid_field(data.get("new_agent_id"))
    person = lock_active_agent(new_agent)
    case = case_row(case_id, lock=True)
    writable(case)
    previous = case["current_agent_id"]
    if previous is None:
        raise ApiError("TICKET_NOT_CLAIMED")
    if "previous_agent_id" in data and previous not in (data["previous_agent_id"], new_agent):
        raise ApiError("ASSIGNMENT_CHANGED")
    if previous != new_agent:
        old = one("SELECT login_id FROM staff_account WHERE staff_id=%s", (previous,))
        admin_change("상담 재배정", f"{old['login_id']} → {person['login_id']}", case_id)
    return set_assignee(case, new_agent, user["staff_id"])


def lock_active_agent(staff_id):
    person = one("SELECT login_id FROM staff_account WHERE staff_id=%s AND role='AGENT' AND is_active=1 FOR UPDATE", (staff_id,))
    if not person:
        raise ApiError("AGENT_UNAVAILABLE")
    return person


@cases.post("/staff/cases/<uuid:case_id>/claim")
def claim(case_id):
    user = agent()
    lock_active_agent(user["staff_id"])
    case = case_row(case_id, lock=True)
    writable(case)
    if case["current_agent_id"] not in (None, user["staff_id"]):
        raise ApiError("TICKET_TAKEN")
    result = set_assignee(case, user["staff_id"], user["staff_id"])
    if request.mimetype == "application/x-www-form-urlencoded":
        return redirect(url_for("detail_page", case_id=case_id), code=303)
    return result


def set_assignee(case, new_agent, changed_by):
    if case["current_agent_id"] != new_agent:
        run(
            "INSERT INTO assignment_history (case_id, previous_agent_id, new_agent_id, changed_by) VALUES (%s,%s,%s,%s)",
            (case["case_id"], case["current_agent_id"], new_agent, changed_by),
        )
        run("UPDATE support_case SET current_agent_id=%s WHERE case_id=%s", (new_agent, case["case_id"]))
        case["current_agent_id"] = new_agent
    return case_info(case)


def phone_info(case_id):
    phone = one(
        "SELECT p.call_at,p.consent_state,p.inquiry,p.guidance,p.followup_required,c.call_direction,c.talk_seconds "
        "FROM phone_response p JOIN support_case c USING(case_id) WHERE p.case_id=%s",
        (case_id,),
    )
    if phone:
        phone["call_at"] = stamp(phone["call_at"])
        if phone["followup_required"] is not None:
            phone["followup_required"] = bool(phone["followup_required"])
    return phone


def assignment_log(case_id):
    assignments = rows(
        "SELECT a.changed_at, s.login_id FROM assignment_history a "
        "LEFT JOIN staff_account s ON s.staff_id=a.new_agent_id "
        "WHERE a.case_id=%s ORDER BY a.assignment_id", (case_id,),
    )
    return [{"login_id": a["login_id"], "at": stamp(a["changed_at"])} for a in assignments]


def staff_notes(case_id=None, *, agent_id=None):
    sql = (
        "SELECT n.note_id,n.case_id,n.body,n.created_at,n.updated_at,a.login_id FROM staff_note n "
        "JOIN staff_account a ON a.staff_id=n.created_by JOIN visible_case c ON c.case_id=n.case_id "
        "WHERE n.body IS NOT NULL AND (c.content_expires_at IS NULL OR c.content_expires_at>UTC_TIMESTAMP(6))"
    )
    args = []
    if case_id is not None:
        sql += " AND n.case_id=%s"
        args.append(case_id)
    if agent_id is not None:
        sql += " AND c.current_agent_id=%s"
        args.append(agent_id)
    notes = rows(sql + " ORDER BY n.note_id DESC", args)
    for note in notes:
        note.update(created_at=stamp(note["created_at"]), updated_at=stamp(note["updated_at"]))
    return notes


@cases.get("/staff/cases/<uuid:case_id>")
def case_detail(case_id):
    g.audit_target = ("CASE_VIEW", case_id, None)
    case = assigned_case(case_id)
    visible = content_visible(case)
    recs = rows("SELECT * FROM recording WHERE case_id=%s ORDER BY expires_at DESC", (case_id,))
    return {
        "case": case_info(case), "result_note": case["result_note"] if visible else None,
        "customer_ref": case["support_subject_ref"] if visible else None, "content_expired": not visible,
        "assignments": assignment_log(case_id),
        "staff_notes": staff_notes(case_id, agent_id=g.actor) if visible else [],
        "phone_response": phone_info(case_id) if visible and case["channel"] == "PHONE" else None,
        "recordings": [recording_info(item) for item in recs],
    }


@cases.post("/staff/cases/<uuid:case_id>/phone-response")
def update_phone(case_id):
    case = assigned_case(case_id, lock=True)
    channel(case, "PHONE")
    writable(case)
    return save_phone_response(case_id, payload())


@cases.post("/staff/cases/<uuid:case_id>/category")
def categorize(case_id):
    case = assigned_case(case_id, lock=True)
    writable(case)
    category = payload().get("inquiry_type")
    if not isinstance(category, str) or category not in TOPICS:
        raise ApiError("INVALID_INPUT")
    run("UPDATE support_case SET inquiry_type=%s WHERE case_id=%s", (category,case_id))
    return {"inquiry_type": category}


def save_phone_response(case_id, data, allow_clear=False):
    metrics = {}
    if "call_direction" in data:
        value = data["call_direction"]
        if not isinstance(value, str) or value not in DIRECTIONS:
            raise ApiError("INVALID_INPUT")
        metrics["call_direction"] = value
    if "talk_seconds" in data:
        value = str(data["talk_seconds"])
        if not value.isascii() or not value.isdecimal() or len(value) > 6 or int(value) > 86400:
            raise ApiError("INVALID_INPUT")
        metrics["talk_seconds"] = int(value)
    edits = {}
    if allow_clear:
        edits = {key: None for key, value in data.items() if value is None}
        data = {key: value for key, value in data.items() if value is not None}
    if "call_at" in data:
        try:
            call_at = datetime.fromisoformat(str(data["call_at"]).replace("Z", "+00:00"))
            if call_at.tzinfo is None:
                raise ValueError
            utc_call = call_at.astimezone(timezone.utc).replace(tzinfo=None)
            if utc_call.year < 1000:
                raise ValueError
            edits["call_at"] = utc_call
        except (ValueError, OverflowError):
            raise ApiError("INVALID_INPUT") from None
    for key in ("inquiry", "guidance"):
        if key in data:
            edits[key] = text_field(data[key])
    if "followup_required" in data:
        if not isinstance(data["followup_required"], bool):
            raise ApiError("INVALID_INPUT")
        edits["followup_required"] = data["followup_required"]
    if "consent_state" in data:
        consent = data["consent_state"]
        if consent not in ("AGREED", "REFUSED"):
            raise ApiError("INVALID_INPUT")
        current = one("SELECT consent_state FROM phone_response WHERE case_id=%s", (case_id,))
        edits["consent_state"] = consent
        if current["consent_state"] != consent:
            edits.update(consent_by=g.actor, consent_at=utcnow())
        if consent == "REFUSED":
            run("UPDATE recording SET state='DELETE_PENDING' WHERE case_id=%s AND state IN ('READY','FAILED','PENDING')", (case_id,))
    if not edits and not metrics:
        raise ApiError("INVALID_INPUT")
    for table, values in (("phone_response", edits), ("support_case", metrics)):
        if values:
            columns = ", ".join(f"{key}=%s" for key in values)
            run(f"UPDATE {table} SET {columns} WHERE case_id=%s", (*values.values(), case_id))
    return phone_info(case_id)


@cases.post("/staff/cases/<uuid:case_id>/close")
def close_case(case_id):
    case = assigned_case(case_id, lock=True)
    writable(case)
    data = payload()
    note = text_field(data.get("result_note"))
    outcome = data.get("outcome")
    if outcome is not None and (not isinstance(outcome, str) or outcome not in OUTCOMES):
        raise ApiError("INVALID_INPUT")
    if case["channel"] == "PHONE":
        phone = phone_info(case_id)
        if (not phone["call_at"] or not phone["inquiry"] or not phone["guidance"]
                or phone["followup_required"] is None or phone["consent_state"] == "UNASKED"):
            raise ApiError("PHONE_RESPONSE_INCOMPLETE")
        if outcome is None:
            outcome = "FOLLOWUP" if phone["followup_required"] else "RESOLVED"
    case["closed_at"] = utcnow()
    run(
        "UPDATE support_case SET closed_at=%s,result_note=%s,outcome=%s,content_expires_at=%s WHERE case_id=%s",
        (case["closed_at"], note, outcome, deadline("CASE_RETENTION_MINUTES", 5), case_id),
    )
    case["outcome"] = outcome
    return case_info(case)


def chat_access(case_id, lock):
    actor = request.headers.get("X-Chat-Actor")
    if actor not in (None, "customer", "inquiry", "staff"):
        raise ApiError("INVALID_INPUT")
    if actor in ("customer", "inquiry") or (actor is None and request.cookies.get(CHAT_COOKIE)):
        session = customer_session(INQUIRY_COOKIE if actor == "inquiry" else CHAT_COOKIE)
        if not session or not session["case_id"]:
            raise ApiError("CHAT_SESSION_REQUIRED")
        if session["case_id"] != case_id:
            raise ApiError("CASE_ACCESS_DENIED")
        return case_row(case_id, lock), None
    case = assigned_case(case_id, lock)
    return case, g.actor


def message_info(row):
    return {
        "message_id": str(row["message_id"]), "author_role": "AGENT" if row["author_staff_id"] else "CUSTOMER",
        "body": row["body"], "sent_at": stamp(row["sent_at"]),
    }


@cases.route("/cases/<uuid:case_id>/messages", methods=["GET", "POST"])
def messages(case_id):
    sending = request.method == "POST"
    if not sending:
        g.audit_target = ("CHAT_VIEW", case_id, None)
    case, author = chat_access(case_id, lock=sending)
    channel(case, "WEB")
    if not sending:
        history = []
        if content_visible(case):
            history = rows("SELECT * FROM chat_message WHERE case_id=%s AND body<>'' ORDER BY message_id", (case_id,))
        return {"items": [message_info(row) for row in history], "read_only": bool(case["closed_at"]), "status": status(case)}
    data = payload()
    message_key = uuid_field(data.get("client_message_id"))
    body = text_field(data.get("body"))
    existing = one("SELECT * FROM chat_message WHERE case_id=%s AND client_message_id=%s", (case_id, message_key))
    if existing and content_visible(case):
        if not existing["body"]:
            raise ApiError("MESSAGE_REMOVED")
        if existing["author_staff_id"] != author or existing["body"] != body:
            raise ApiError("REQUEST_KEY_CONFLICT")
        return message_info(existing)
    writable(case)
    msg_id = run(
        "INSERT INTO chat_message (case_id, client_message_id, author_staff_id, body) VALUES (%s,%s,%s,%s)",
        (case_id, message_key, author, body),
    )
    return message_info(one("SELECT * FROM chat_message WHERE message_id=%s", (msg_id,))), 201
