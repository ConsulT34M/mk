import hmac
import os
import secrets
from datetime import datetime, timezone

from flask import Blueprint, g, request

from common import (
    CHAT_COOKIE, ApiError, admin, assigned_case, case_info, case_row, channel,
    content_visible, customer_session, deadline, digest, payload, staff_user, status,
    text_field, uuid_field, writable,
)
from db import new_id, one, rows, run, stamp, utcnow
from recordings import recording_info

cases = Blueprint("cases", __name__)


def partner():
    provided = request.headers.get("Authorization", "").encode()
    expected = ("Bearer " + os.environ["PARTNER_TOKEN"]).encode()
    if not hmac.compare_digest(provided, expected):
        raise ApiError("CLIENT_AUTH_REQUIRED")
    g.actor = "exchange"


@cases.route("/cases", methods=["GET", "POST"])
def exchange_cases():
    partner()
    if request.method == "GET":
        subject = uuid_field(request.args.get("support_subject_ref"))
        items = rows("SELECT * FROM support_case WHERE support_subject_ref=%s ORDER BY opened_at DESC", (subject,))
        return {"items": [{"case_id": case["case_id"], "status": status(case)} for case in items]}

    data = payload()
    if not data.get("intake_request_id"):
        raise ApiError("MISSING_INTAKE_REQUEST_ID")
    subject = uuid_field(data.get("support_subject_ref"))
    intake = uuid_field(data["intake_request_id"])
    candidate = new_id()
    run(
        "INSERT INTO support_case (case_id, channel, support_subject_ref, intake_request_id) "
        "VALUES (%s, 'WEB', %s, %s) ON DUPLICATE KEY UPDATE case_id=case_id",
        (candidate, subject, intake),
    )
    case = one("SELECT case_id, support_subject_ref FROM support_case WHERE intake_request_id=%s", (intake,))
    if case["support_subject_ref"] != subject:
        raise ApiError("REQUEST_KEY_CONFLICT")
    return {"case_id": case["case_id"]}, 201 if case["case_id"] == candidate else 200


@cases.post("/cases/<uuid:case_id>/entry-codes")
def grant_entry(case_id):
    partner()
    subject = uuid_field(payload().get("support_subject_ref"))
    case = case_row(case_id)
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
        result = rows("SELECT * FROM support_case ORDER BY opened_at DESC")
    else:
        result = rows("SELECT * FROM support_case WHERE current_agent_id=%s ORDER BY opened_at DESC", (user["staff_id"],))
    return {"items": [case_info(case) for case in result]}


@cases.get("/staff/agents")
def agents():
    admin()
    return {"items": rows("SELECT staff_id, login_id FROM staff_account WHERE role='AGENT' AND is_active=1 ORDER BY login_id")}


@cases.post("/staff/phone-cases")
def new_phone_case():
    admin()
    case_id = new_id()
    run("INSERT INTO support_case (case_id, channel) VALUES (%s, 'PHONE')", (case_id,))
    run("INSERT INTO phone_response (case_id) VALUES (%s)", (case_id,))
    return {"case_id": case_id}, 201


@cases.post("/staff/cases/<uuid:case_id>/assignee")
def assign(case_id):
    user = admin()
    new_agent = uuid_field(payload().get("new_agent_id"))
    case = case_row(case_id, lock=True)
    writable(case)
    if not one("SELECT staff_id FROM staff_account WHERE staff_id=%s AND role='AGENT' AND is_active=1", (new_agent,)):
        raise ApiError("AGENT_UNAVAILABLE")
    if case["current_agent_id"] != new_agent:
        run(
            "INSERT INTO assignment_history (case_id, previous_agent_id, new_agent_id, changed_by) VALUES (%s,%s,%s,%s)",
            (case_id, case["current_agent_id"], new_agent, user["staff_id"]),
        )
        run("UPDATE support_case SET current_agent_id=%s WHERE case_id=%s", (new_agent, case_id))
        case["current_agent_id"] = new_agent
    return case_info(case)


def phone_info(case_id):
    phone = one(
        "SELECT call_at, consent_state, inquiry, guidance, followup_required FROM phone_response WHERE case_id=%s",
        (case_id,),
    )
    if phone:
        phone["call_at"] = stamp(phone["call_at"])
        if phone["followup_required"] is not None:
            phone["followup_required"] = bool(phone["followup_required"])
    return phone


@cases.get("/staff/cases/<uuid:case_id>")
def case_detail(case_id):
    g.audit_target = ("CASE_VIEW", case_id, None)
    case = assigned_case(case_id)
    visible = content_visible(case)
    recs = rows("SELECT * FROM recording WHERE case_id=%s ORDER BY expires_at DESC", (case_id,))
    return {
        "case": case_info(case), "result_note": case["result_note"] if visible else None,
        "phone_response": phone_info(case_id) if visible and case["channel"] == "PHONE" else None,
        "recordings": [recording_info(item) for item in recs],
    }


@cases.post("/staff/cases/<uuid:case_id>/phone-response")
def update_phone(case_id):
    case = assigned_case(case_id, lock=True)
    channel(case, "PHONE")
    writable(case)
    data = payload()
    edits = {}
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
    if not edits:
        raise ApiError("INVALID_INPUT")
    columns = ", ".join(f"{key}=%s" for key in edits)
    run(f"UPDATE phone_response SET {columns} WHERE case_id=%s", (*edits.values(), case_id))
    return phone_info(case_id)


@cases.post("/staff/cases/<uuid:case_id>/close")
def close_case(case_id):
    case = assigned_case(case_id, lock=True)
    writable(case)
    note = text_field(payload().get("result_note"))
    if case["channel"] == "PHONE":
        phone = phone_info(case_id)
        if (not phone["call_at"] or not phone["inquiry"] or not phone["guidance"]
                or phone["followup_required"] is None or phone["consent_state"] == "UNASKED"):
            raise ApiError("PHONE_RESPONSE_INCOMPLETE")
    case["closed_at"] = utcnow()
    run(
        "UPDATE support_case SET closed_at=%s, result_note=%s, content_expires_at=%s WHERE case_id=%s",
        (case["closed_at"], note, deadline("CASE_RETENTION_MINUTES", 5), case_id),
    )
    return case_info(case)


def chat_access(case_id, lock):
    actor = request.headers.get("X-Chat-Actor")
    if actor not in (None, "customer", "staff"):
        raise ApiError("INVALID_INPUT")
    if actor == "customer" or (actor is None and request.cookies.get(CHAT_COOKIE)):
        session = customer_session()
        if not session:
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
            history = rows("SELECT * FROM chat_message WHERE case_id=%s ORDER BY message_id", (case_id,))
        return {"items": [message_info(row) for row in history], "read_only": bool(case["closed_at"])}
    data = payload()
    message_key = uuid_field(data.get("client_message_id"))
    body = text_field(data.get("body"))
    existing = one("SELECT * FROM chat_message WHERE case_id=%s AND client_message_id=%s", (case_id, message_key))
    if existing and content_visible(case):
        if existing["author_staff_id"] != author or existing["body"] != body:
            raise ApiError("REQUEST_KEY_CONFLICT")
        return message_info(existing)
    writable(case)
    msg_id = run(
        "INSERT INTO chat_message (case_id, client_message_id, author_staff_id, body) VALUES (%s,%s,%s,%s)",
        (case_id, message_key, author, body),
    )
    return message_info(one("SELECT * FROM chat_message WHERE message_id=%s", (msg_id,))), 201
