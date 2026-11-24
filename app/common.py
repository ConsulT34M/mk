import hashlib
import os
from datetime import timedelta
from uuid import UUID

from flask import g, request

from .db import one, stamp, utcnow

STAFF_COOKIE = "tm_staff"
CHAT_COOKIE = "tm_chat"
ERRORS = {
    "INVALID_INPUT": (400, "입력 항목을 확인해 주세요."),
    "MISSING_INTAKE_REQUEST_ID": (400, "접수 요청 번호가 필요합니다."),
    "CLIENT_AUTH_REQUIRED": (401, "거래소 연동 자격을 확인해 주세요."),
    "STAFF_AUTH_REQUIRED": (401, "직원 로그인 정보를 확인해 주세요."),
    "CHAT_SESSION_REQUIRED": (401, "거래소에서 새 입장 코드를 받아 주세요."),
    "CASE_ACCESS_DENIED": (403, "해당 작업을 수행할 권한이 없습니다."),
    "CASE_NOT_FOUND": (404, "해당 상담을 찾을 수 없습니다."),
    "TICKET_DELETED": (410, "삭제된 상담 티켓입니다."),
    "RECORDING_NOT_FOUND": (404, "해당 녹취를 찾을 수 없습니다."),
    "REQUEST_KEY_CONFLICT": (409, "이미 다른 내용으로 사용한 요청 번호입니다."),
    "TICKET_TAKEN": (409, "다른 상담원이 먼저 받은 티켓입니다."),
    "TICKET_NOT_CLAIMED": (409, "상담원이 티켓을 받은 뒤에 담당자를 변경할 수 있습니다."),
    "CASE_READ_ONLY": (409, "종결된 상담은 조회만 가능합니다."),
    "CHANNEL_MISMATCH": (409, "이 상담 채널에서 사용할 수 없는 기능입니다."),
    "PHONE_RESPONSE_INCOMPLETE": (422, "통화 일시, 녹취 안내 결과, 문의·안내 내용과 후속 조치를 입력해 주세요."),
    "AGENT_UNAVAILABLE": (422, "배정할 수 있는 활성 상담원을 선택해 주세요."),
    "ASSIGNMENT_CHANGED": (409, "그사이 담당자가 변경되었습니다. 목록을 새로고침한 뒤 다시 선택해 주세요."),
    "AGENT_NOT_FOUND": (404, "상담원 계정을 찾을 수 없습니다."),
    "LOGIN_ID_TAKEN": (409, "이미 사용 중인 직원 아이디입니다."),
    "MESSAGE_NOT_FOUND": (404, "채팅 메시지를 찾을 수 없습니다."),
    "STAFF_NOTE_NOT_FOUND": (404, "직원용 상담 기록을 찾을 수 없습니다."),
    "STAFF_NOTE_CHANGED": (409, "다른 관리자가 기록을 수정했습니다. 새로고침 후 내용을 확인해 주세요."),
    "MESSAGE_REMOVED": (410, "삭제된 메시지입니다."),
    "CONTENT_EXPIRED": (410, "상담 기록의 보관 기한이 지났습니다."),
    "RECORDING_CONSENT_REQUIRED": (409, "녹취에 동의한 전화 상담에만 등록할 수 있습니다."),
    "RECORDING_NOT_READY": (409, "녹취 저장 상태를 확인해 주세요."),
    "ENTRY_CODE_UNUSABLE": (410, "거래소에서 새 입장 코드를 받아 주세요."),
    "RECORDING_EXPIRED": (410, "녹취 보관 기한이 지났습니다."),
    "RECORDING_TOO_LARGE": (413, "녹취 파일이 허용 크기를 초과합니다."),
    "WAV_FORMAT_UNSUPPORTED": (415, "유효한 PCM WAV 파일을 선택해 주세요."),
    "RECORDING_STORE_FAILED": (503, "저장소에 접근할 수 없습니다. 같은 요청으로 다시 시도해 주세요."),
}


class ApiError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
        self.status, self.message = ERRORS[code]


def payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("INVALID_INPUT")
    return data


def uuid_field(value):
    if not isinstance(value, str):
        raise ApiError("INVALID_INPUT")
    try:
        return str(UUID(value))
    except ValueError:
        raise ApiError("INVALID_INPUT") from None


def text_field(value, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ApiError("INVALID_INPUT")
    return value.strip()


def digest(value):
    return hashlib.sha256(value.encode()).digest()


def deadline(setting, default):
    return utcnow() + timedelta(minutes=int(os.getenv(setting, default)))


def staff_user(required=True):
    token = request.cookies.get(STAFF_COOKIE)
    user = None
    if token:
        user = one(
            "SELECT a.staff_id, a.login_id, a.role FROM staff_session s "
            "JOIN staff_account a ON a.staff_id=s.staff_id "
            "WHERE s.token_hash=%s AND s.expires_at>UTC_TIMESTAMP(6) AND a.is_active=1",
            (digest(token),),
        )
    if user:
        g.actor = user["staff_id"]
    elif required:
        raise ApiError("STAFF_AUTH_REQUIRED")
    return user


def customer_session():
    token = request.cookies.get(CHAT_COOKIE)
    if token:
        row = one(
            "SELECT case_id, expires_at FROM customer_session "
            "WHERE token_hash=%s AND expires_at>UTC_TIMESTAMP(6)", (digest(token),),
        )
        if row:
            g.actor = "customer:" + row["case_id"]
            return row
    return None


def admin():
    user = staff_user()
    if user["role"] != "ADMIN":
        raise ApiError("CASE_ACCESS_DENIED")
    return user


def agent():
    user = staff_user()
    if user["role"] != "AGENT":
        raise ApiError("CASE_ACCESS_DENIED")
    return user


def case_row(case_id, lock=False):
    sql = "SELECT * FROM visible_case WHERE case_id=%s"
    case = one(sql + (" FOR UPDATE" if lock else ""), (case_id,))
    if not case:
        raise ApiError("CASE_NOT_FOUND")
    return case


def assigned_case(case_id, lock=False):
    user = staff_user()
    case = case_row(case_id, lock)
    if user["role"] != "AGENT" or case["current_agent_id"] != user["staff_id"]:
        raise ApiError("CASE_ACCESS_DENIED")
    return case


def writable(case):
    if case["closed_at"]:
        raise ApiError("CASE_READ_ONLY")


def channel(case, expected):
    if case["channel"] != expected:
        raise ApiError("CHANNEL_MISMATCH")


def status(case):
    if case["closed_at"]:
        return "CLOSED"
    return "IN_PROGRESS" if case["current_agent_id"] else "QUEUED"


def case_info(case):
    return {
        "case_id": case["case_id"], "channel": case["channel"], "status": status(case),
        "current_agent_id": case["current_agent_id"], "opened_at": stamp(case["opened_at"]),
        "closed_at": stamp(case["closed_at"]),
    }


def content_visible(case):
    return case["content_expires_at"] is None or case["content_expires_at"] > utcnow()
