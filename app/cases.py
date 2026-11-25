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

# 거래소의 접수 API, 직원 데스크, 고객 채팅이 같은 티켓 데이터를 사용한다.
# 각 라우트는 호출 주체에 맞는 인증을 거친 뒤 아래의 접수·배정·응대 함수를 호출한다.
cases = Blueprint("cases", __name__)


def partner():
    # 거래소의 서버 간 요청은 Authorization 헤더의 Bearer 토큰으로 확인한다.
    # 인증이 끝나면 이 요청의 작업 주체를 거래소로 표시한다.
    provided = request.headers.get("Authorization", "").encode()
    expected = ("Bearer " + os.environ["PARTNER_TOKEN"]).encode()
    if not hmac.compare_digest(provided, expected):
        raise ApiError("CLIENT_AUTH_REQUIRED")
    g.actor = "exchange"


def intake_channel(value):
    # 티켓을 생성하거나 채널별로 조회할 때 허용하는 두 채널 코드를 검사한다.
    if value not in ("WEB", "PHONE"):
        raise ApiError("INVALID_INPUT")
    return value


@cases.route("/cases", methods=["GET", "POST"])
def exchange_cases():
    # GET은 고객 참조값과 채널로 기존 티켓의 ID·상태만 조회한다. 최신 접수 건이 먼저 온다.
    partner()
    if request.method == "GET":
        subject = uuid_field(request.args.get("support_subject_ref"))
        kind = intake_channel(request.args.get("channel", "WEB"))
        items = rows(
            "SELECT * FROM visible_case WHERE support_subject_ref=%s AND channel=%s ORDER BY opened_at DESC,case_id",
            (subject, kind),
        )
        return {"items": [{"case_id": case["case_id"], "status": status(case)} for case in items]}

    # POST는 거래소의 접수 요청 ID를 필수로 받는다. Flask는 반환한 딕셔너리를 JSON으로 만들고,
    # 튜플의 두 번째 값을 HTTP 상태로 쓴다. 새 티켓은 201, 같은 접수의 재시도는 200이다.
    data = payload()
    if not data.get("intake_request_id"):
        raise ApiError("MISSING_INTAKE_REQUEST_ID")
    subject = uuid_field(data.get("support_subject_ref"))
    intake = uuid_field(data["intake_request_id"])
    kind = intake_channel(data.get("channel", "WEB"))
    case_id, created = intake_case(subject, intake, kind, data.get("inquiry_type"))
    return {"case_id": case_id}, 201 if created else 200


def intake_case(subject, intake, kind, inquiry_type=None):
    # subject는 고객 참조값, intake는 접수 시도에 붙인 고유 키다. 먼저 삽입을 시도하고
    # 고유 키로 실제 저장된 행을 읽어, 동시에 같은 접수가 들어와도 같은 티켓 ID를 돌려준다.
    if inquiry_type is not None and (not isinstance(inquiry_type, str) or inquiry_type not in TOPICS):
        raise ApiError("INVALID_INPUT")
    candidate = new_id()
    run(
        "INSERT INTO support_case (case_id, channel, support_subject_ref, intake_request_id,inquiry_type) "
        "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE case_id=case_id",
        (candidate, kind, subject, intake, inquiry_type),
    )
    case = one("SELECT case_id,channel,support_subject_ref,deleted_at FROM support_case WHERE intake_request_id=%s", (intake,))
    # 삭제된 티켓의 키로 다시 접수할 수 없다. 기존 키의 고객 또는 채널이 다르면 키 충돌이다.
    if case["deleted_at"]:
        raise ApiError("TICKET_DELETED")
    if case["support_subject_ref"] != subject or case["channel"] != kind:
        raise ApiError("REQUEST_KEY_CONFLICT")
    created = case["case_id"] == candidate
    # 이번 호출이 새 전화 티켓을 만든 경우에만 응대 내용을 채울 빈 행도 한 번 생성한다.
    if created and kind == "PHONE":
        run("INSERT INTO phone_response (case_id) VALUES (%s)", (candidate,))
    return case["case_id"], created


@cases.post("/cases/<uuid:case_id>/entry-codes")
def grant_entry(case_id):
    # 거래소가 제시한 고객 참조값과 웹 티켓의 소유 관계를 확인한 뒤 일회용 입장 코드를 만든다.
    # 원문과 만료 시각은 이 응답에 담고, 추후 코드 교환에 필요한 해시를 DB에 저장한다.
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
    # 관리자는 전체 티켓을, 상담원은 현재 본인에게 배정된 티켓을 최신 접수순으로 받는다.
    user = staff_user()
    g.audit_target = ("CASE_LIST", None, None)
    if user["role"] == "ADMIN":
        result = rows("SELECT * FROM visible_case ORDER BY opened_at DESC")
    else:
        result = rows("SELECT * FROM visible_case WHERE current_agent_id=%s ORDER BY opened_at DESC", (user["staff_id"],))
    return {"items": [case_info(case) for case in result]}


@cases.get("/staff/agents")
def agents():
    # 관리자 재배정 화면에서 고를 수 있는 활성 상담원의 ID와 로그인 이름만 반환한다.
    admin()
    return {"items": rows("SELECT staff_id, login_id FROM staff_account WHERE role='AGENT' AND is_active=1 ORDER BY login_id")}


def waiting_tickets():
    # 아직 종결되지 않은 미배정 티켓을 대기열에 넣는다. 처음 대기하는 경우 접수 시각,
    # 담당자가 해제되어 돌아온 경우 마지막 배정 해제 시각부터 기다린 것으로 계산한다.
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
    # 담당 티켓의 미리보기는 전화라면 문의 내용, 웹이라면 삭제되지 않은 마지막 메시지의 앞 100자다.
    # 보관 기한이 지나면 목록에는 티켓 정보와 만료 여부만 남기고 미리보기는 비운다.
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
    # 담당자의 최근 종결 티켓 여섯 건에 아직 재생 가능한 최신 녹취 한 건의 ID를 붙인다.
    items = rows(
        "SELECT c.*, (SELECT r.recording_id FROM recording r WHERE r.case_id=c.case_id AND r.state='READY' "
        "AND r.expires_at>UTC_TIMESTAMP(6) ORDER BY r.stored_at DESC,r.recording_id LIMIT 1) AS clip_id "
        "FROM visible_case c WHERE c.current_agent_id=%s AND c.closed_at IS NOT NULL "
        "ORDER BY c.closed_at DESC,c.case_id LIMIT 6", (staff_id,),
    )
    return [dict(case_info(item), clip_id=item["clip_id"]) for item in items]


@cases.post("/staff/cases/<uuid:case_id>/assignee")
def assign(case_id):
    # 관리자의 재배정은 활성 상담원 계정과 티켓을 차례로 잠근 상태에서 처리한다.
    # 대기 중인 티켓의 최초 배정은 상담원이 직접 수락하는 claim이 담당한다.
    user = admin()
    data = payload()
    new_agent = uuid_field(data.get("new_agent_id"))
    person = lock_active_agent(new_agent)
    case = case_row(case_id, lock=True)
    writable(case)
    previous = case["current_agent_id"]
    if previous is None:
        raise ApiError("TICKET_NOT_CLAIMED")
    # 화면이 previous_agent_id를 함께 보내면 DB의 현재 담당자와 대조해 중간에 바뀐 배정을 감지한다.
    # 현재 담당자가 이미 요청한 새 담당자라면 같은 재배정의 재시도로 받아들인다.
    if "previous_agent_id" in data and previous not in (data["previous_agent_id"], new_agent):
        raise ApiError("ASSIGNMENT_CHANGED")
    if previous != new_agent:
        old = one("SELECT login_id FROM staff_account WHERE staff_id=%s", (previous,))
        admin_change("상담 재배정", f"{old['login_id']} → {person['login_id']}", case_id)
    return set_assignee(case, new_agent, user["staff_id"])


def lock_active_agent(staff_id):
    # 계정 비활성화와 배정이 겹쳐도 일관된 순서로 진행하도록, 활성 상담원 행에 쓰기 잠금을 건다.
    person = one("SELECT login_id FROM staff_account WHERE staff_id=%s AND role='AGENT' AND is_active=1 FOR UPDATE", (staff_id,))
    if not person:
        raise ApiError("AGENT_UNAVAILABLE")
    return person


@cases.post("/staff/cases/<uuid:case_id>/claim")
def claim(case_id):
    # 티켓을 잠근 뒤 담당자를 검사한다. 동시 수락은 한 상담원만 성공하고
    # 이미 본인에게 배정된 티켓을 다시 수락하면 현재 배정 결과를 돌려준다.
    user = agent()
    lock_active_agent(user["staff_id"])
    case = case_row(case_id, lock=True)
    writable(case)
    if case["current_agent_id"] not in (None, user["staff_id"]):
        raise ApiError("TICKET_TAKEN")
    result = set_assignee(case, user["staff_id"], user["staff_id"])
    # 폼 제출은 상세 화면으로 이동하고 API 호출은 배정이 반영된 티켓 정보를 받는다.
    if request.mimetype == "application/x-www-form-urlencoded":
        return redirect(url_for("detail_page", case_id=case_id), code=303)
    return result


def set_assignee(case, new_agent, changed_by):
    # 수락·재배정·해제가 공유하는 갱신이다. 담당자가 실제로 달라질 때만 이력을 추가하고
    # 현재 담당자를 변경한다. new_agent가 None이면 대기열로 돌아간다.
    if case["current_agent_id"] != new_agent:
        run(
            "INSERT INTO assignment_history (case_id, previous_agent_id, new_agent_id, changed_by) VALUES (%s,%s,%s,%s)",
            (case["case_id"], case["current_agent_id"], new_agent, changed_by),
        )
        run("UPDATE support_case SET current_agent_id=%s WHERE case_id=%s", (new_agent, case["case_id"]))
        case["current_agent_id"] = new_agent
    return case_info(case)


def phone_info(case_id):
    # 통화 일시·동의·안내 내역과 티켓에 저장한 통화 방향·길이를 합쳐 하나의 전화 응대로 반환한다.
    # 후속 조치의 NULL은 아직 입력하지 않았다는 뜻이므로 False와 구분한다.
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
    # 변경 순서대로 배정 시각과 새 담당자의 로그인 이름을 반환한다. 배정 해제 행은 이름이 비어 있다.
    assignments = rows(
        "SELECT a.changed_at, s.login_id FROM assignment_history a "
        "LEFT JOIN staff_account s ON s.staff_id=a.new_agent_id "
        "WHERE a.case_id=%s ORDER BY a.assignment_id", (case_id,),
    )
    return [{"login_id": a["login_id"], "at": stamp(a["changed_at"])} for a in assignments]


def staff_notes(case_id=None, *, agent_id=None):
    # 보관 기한 안의 티켓에서 본문이 남은 직원 메모만 읽는다. 관리자 전체 이력에는 기본 범위를,
    # 상담원 화면에는 티켓 ID와 현재 담당자 ID 조건을 함께 적용한다.
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
    # 요청한 티켓을 감사 대상으로 먼저 지정해 권한 검사 실패도 기록할 수 있게 한다.
    # 담당 여부를 확인한 뒤 티켓 상태·배정 이력과, 기한 내에만 보여 줄 상담 본문을 조립한다.
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
    # 현재 담당자가 열린 전화 티켓을 수정하는지 확인한 뒤 POST로 받은 필드만 반영한다.
    case = assigned_case(case_id, lock=True)
    channel(case, "PHONE")
    writable(case)
    return save_phone_response(case_id, payload())


@cases.post("/staff/cases/<uuid:case_id>/category")
def categorize(case_id):
    # 열린 담당 티켓의 문의 유형을 공통 분류표의 코드로 저장하고 화면에서 사용할 값을 돌려준다.
    case = assigned_case(case_id, lock=True)
    writable(case)
    category = payload().get("inquiry_type")
    if not isinstance(category, str) or category not in TOPICS:
        raise ApiError("INVALID_INPUT")
    run("UPDATE support_case SET inquiry_type=%s WHERE case_id=%s", (category,case_id))
    return {"inquiry_type": category}


def save_phone_response(case_id, data, allow_clear=False):
    # 통화 방향·길이는 통계가 읽는 support_case에, 통화 일시·문의·안내·동의는 phone_response에 저장한다.
    # 부분 수정이므로 실제 요청에 담긴 필드만 두 묶음에 수집한다.
    metrics = {}
    if "call_direction" in data:
        value = data["call_direction"]
        if not isinstance(value, str) or value not in DIRECTIONS:
            raise ApiError("INVALID_INPUT")
        metrics["call_direction"] = value
    if "talk_seconds" in data:
        # 통화 길이는 초 단위 정수이며 하루(86,400초)까지 받는다.
        value = str(data["talk_seconds"])
        if not value.isascii() or not value.isdecimal() or len(value) > 6 or int(value) > 86400:
            raise ApiError("INVALID_INPUT")
        metrics["talk_seconds"] = int(value)
    edits = {}
    if allow_clear:
        # 관리자 이력 수정은 비울 수 있는 필드만 골라 전달한다. 명시한 null은 열을 비우고,
        # 요청에서 빠진 필드는 기존 값을 유지한다.
        edits = {key: None for key, value in data.items() if value is None}
        data = {key: value for key, value in data.items() if value is not None}
    if "call_at" in data:
        # 시간대가 있는 ISO 일시를 UTC로 바꿔 MySQL DATETIME 범위에 맞는 값으로 저장한다.
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
        # 동의 상태가 바뀔 때만 처리자와 시각을 기록한다. 거부하면 기존 녹취도 삭제 대기로
        # 전환해 재생을 막고, 실제 파일 삭제는 정리 작업이 수행한다.
        edits["consent_state"] = consent
        if current["consent_state"] != consent:
            edits.update(consent_by=g.actor, consent_at=utcnow())
        if consent == "REFUSED":
            run("UPDATE recording SET state='DELETE_PENDING' WHERE case_id=%s AND state IN ('READY','FAILED','PENDING')", (case_id,))
    if not edits and not metrics:
        raise ApiError("INVALID_INPUT")
    for table, values in (("phone_response", edits), ("support_case", metrics)):
        # 값을 담은 테이블만 UPDATE한 뒤 전화 응대 전체를 다시 읽어 호출자에게 돌려준다.
        if values:
            columns = ", ".join(f"{key}=%s" for key in values)
            run(f"UPDATE {table} SET {columns} WHERE case_id=%s", (*values.values(), case_id))
    return phone_info(case_id)


@cases.post("/staff/cases/<uuid:case_id>/close")
def close_case(case_id):
    # 종결 메모는 모든 채널에서 필요하다. 결과 코드가 있다면 공통 결과 목록에 있는지 검사한다.
    case = assigned_case(case_id, lock=True)
    writable(case)
    data = payload()
    note = text_field(data.get("result_note"))
    outcome = data.get("outcome")
    if outcome is not None and (not isinstance(outcome, str) or outcome not in OUTCOMES):
        raise ApiError("INVALID_INPUT")
    if case["channel"] == "PHONE":
        # 전화는 필수 응대 기록과 녹취 안내 결과를 모두 입력해야 종결할 수 있다.
        # 결과를 생략하면 후속 조치 필요 여부에서 FOLLOWUP 또는 RESOLVED를 결정한다.
        phone = phone_info(case_id)
        if (not phone["call_at"] or not phone["inquiry"] or not phone["guidance"]
                or phone["followup_required"] is None or phone["consent_state"] == "UNASKED"):
            raise ApiError("PHONE_RESPONSE_INCOMPLETE")
        if outcome is None:
            outcome = "FOLLOWUP" if phone["followup_required"] else "RESOLVED"
    case["closed_at"] = utcnow()
    # 종결 시점에 본문 보관 기한도 정한다. 조회 경로와 만료 정리 작업이 이 기한을 함께 사용한다.
    run(
        "UPDATE support_case SET closed_at=%s,result_note=%s,outcome=%s,content_expires_at=%s WHERE case_id=%s",
        (case["closed_at"], note, outcome, deadline("CASE_RETENTION_MINUTES", 5), case_id),
    )
    case["outcome"] = outcome
    return case_info(case)


def chat_access(case_id, lock):
    # 고객 채팅, 공개 문의, 직원 화면이 하나의 메시지 API를 공유한다.
    # X-Chat-Actor는 확인할 세션을 지정하며, 각 경로에서 실제 쿠키나 담당자 배정을 검증한다.
    actor = request.headers.get("X-Chat-Actor")
    if actor not in (None, "customer", "inquiry", "staff"):
        raise ApiError("INVALID_INPUT")
    if actor in ("customer", "inquiry") or (actor is None and request.cookies.get(CHAT_COOKIE)):
        # 역할을 명시하지 않고 거래소 채팅 쿠키가 있는 기존 호출도 고객 경로로 받는다.
        session = customer_session(INQUIRY_COOKIE if actor == "inquiry" else CHAT_COOKIE)
        if not session or not session["case_id"]:
            raise ApiError("CHAT_SESSION_REQUIRED")
        if session["case_id"] != case_id:
            raise ApiError("CASE_ACCESS_DENIED")
        # 고객이 보낸 메시지는 author_staff_id가 NULL이다. 직원 경로는 확인한 상담원의 ID를 반환한다.
        return case_row(case_id, lock), None
    case = assigned_case(case_id, lock)
    return case, g.actor


def message_info(row):
    # 메시지 번호는 문자열로 직렬화하고 직원 ID 유무로 채팅 화면이 쓸 발신자 역할을 정한다.
    return {
        "message_id": str(row["message_id"]), "author_role": "AGENT" if row["author_staff_id"] else "CUSTOMER",
        "body": row["body"], "sent_at": stamp(row["sent_at"]),
    }


@cases.route("/cases/<uuid:case_id>/messages", methods=["GET", "POST"])
def messages(case_id):
    # 읽기와 전송 모두 웹 티켓 권한을 확인한다. 전송은 티켓을 잠가 종결·재배정과 처리 순서를 맞춘다.
    sending = request.method == "POST"
    if not sending:
        g.audit_target = ("CHAT_VIEW", case_id, None)
    case, author = chat_access(case_id, lock=sending)
    channel(case, "WEB")
    if not sending:
        history = []
        # 본문 기한이 지나면 빈 목록을 반환하되, 화면이 갱신할 읽기 전용 여부와 티켓 상태는 전달한다.
        if content_visible(case):
            history = rows("SELECT * FROM chat_message WHERE case_id=%s AND body<>'' ORDER BY message_id", (case_id,))
        return {"items": [message_info(row) for row in history], "read_only": bool(case["closed_at"]), "status": status(case)}
    data = payload()
    message_key = uuid_field(data.get("client_message_id"))
    body = text_field(data.get("body"))
    existing = one("SELECT * FROM chat_message WHERE case_id=%s AND client_message_id=%s", (case_id, message_key))
    # 서버 응답을 못 받아 같은 전송을 재시도할 수 있다. 기한 안에 같은 키·작성자·본문이 확인되면
    # 종결 여부를 검사하기 전에 기존 메시지를 반환한다. 삭제된 본문이나 내용이 다른 재사용은 오류다.
    if existing and content_visible(case):
        if not existing["body"]:
            raise ApiError("MESSAGE_REMOVED")
        if existing["author_staff_id"] != author or existing["body"] != body:
            raise ApiError("REQUEST_KEY_CONFLICT")
        return message_info(existing)
    # 새 메시지는 열린 티켓에서만 저장하며, 저장한 행의 서버 시각과 ID를 201 응답에 담는다.
    writable(case)
    msg_id = run(
        "INSERT INTO chat_message (case_id, client_message_id, author_staff_id, body) VALUES (%s,%s,%s,%s)",
        (case_id, message_key, author, body),
    )
    return message_info(one("SELECT * FROM chat_message WHERE message_id=%s", (msg_id,))), 201
