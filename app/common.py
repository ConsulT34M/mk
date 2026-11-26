import hashlib
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

from flask import g, request

from .db import stamp, utcnow
from .queries import cases as case_queries
from .queries import chat as chat_queries
from .queries import staff as staff_queries

# 직원 포털, 거래소를 통한 채팅 입장, 공개 문의 페이지는 서로 다른 쿠키로 세션을 찾는다.
STAFF_COOKIE = "tm_staff"
CHAT_COOKIE = "tm_chat"
INQUIRY_COOKIE = "__Host-tm_inquiry"
# 뷰는 업무 오류 코드만 지정한다. HTTP 상태와 사용자에게 표시할 문구는 여기서 짝을 맞춘다.
ERRORS = {
    "AI_CHAT_DISABLED": (409, "AI 채팅 상담 운영이 중지되어 있습니다."),
    "AI_CHAT_NOT_CONFIGURED": (503, "AI 채팅 서비스를 사용할 수 없습니다. 관리자에게 문의해 주세요."),
    "AI_CHAT_LIMIT_REACHED": (429, "AI 채팅 사용 한도에 도달했어요. 잠시 후 다시 시도해 주세요."),
    "AI_CHAT_UNAVAILABLE": (502, "AI 답변을 받지 못했어요. 잠시 후 다시 시도해 주세요."),
    "INVALID_INPUT": (400, "입력 항목을 확인해 주세요."),
    "VOICE_NOT_CONFIGURED": (503, "서버에 OpenAI API 키를 설정하면 음성 상담을 시작할 수 있어요."),
    "VOICE_VISITOR_EXPIRED": (401, "문의 페이지를 새로고침한 뒤 음성 상담을 다시 시작해 주세요."),
    "VOICE_SESSION_NOT_FOUND": (404, "종료되었거나 찾을 수 없는 음성 상담이에요."),
    "VOICE_AUTH_FAILED": (503, "음성 서비스의 API 키와 모델 접근 권한을 확인해 주세요."),
    "VOICE_LIMIT_REACHED": (429, "음성 서비스 사용 한도에 도달했어요. 잠시 후 다시 시도해 주세요."),
    "VOICE_UNAVAILABLE": (502, "음성 서비스에 연결할 수 없어요. 잠시 후 다시 시도해 주세요."),
    "VOICE_RECORDING_ENDED": (409, "종료된 녹취입니다."),
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
    "TEAM_NAME_TAKEN": (409, "이미 사용하는 팀 이름입니다."),
    "TEAM_CHANGED": (409, "팀 구성이 변경되었습니다. 새로고침한 뒤 다시 선택해 주세요."),
    "VOICE_DISABLED": (503, "현재 AI 음성 상담 운영이 중지되어 있습니다."),
    "MESSAGE_NOT_FOUND": (404, "채팅 메시지를 찾을 수 없습니다."),
    "BOARD_POST_NOT_FOUND": (404, "게시글을 찾을 수 없습니다."),
    "BOARD_POST_CHANGED": (409, "게시글이 변경됐습니다. 다시 열어 내용을 확인해 주세요."),
    "STAFF_NOTE_NOT_FOUND": (404, "직원용 상담 기록을 찾을 수 없습니다."),
    "STAFF_NOTE_CHANGED": (409, "다른 관리자가 기록을 수정했습니다. 새로고침 후 내용을 확인해 주세요."),
    "MESSAGE_REMOVED": (410, "삭제된 메시지입니다."),
    "CONTENT_EXPIRED": (410, "상담 기록의 보관 기한이 지났습니다."),
    "RECORDING_CONSENT_REQUIRED": (409, "녹취에 동의한 전화 상담에만 등록할 수 있습니다."),
    "RECORDING_NOT_READY": (409, "녹취 저장 상태를 확인해 주세요."),
    "ENTRY_CODE_UNUSABLE": (410, "거래소에서 새 입장 코드를 받아 주세요."),
    "RECORDING_EXPIRED": (410, "삭제된 녹취입니다."),
    "RECORDING_TOO_LARGE": (413, "녹취 파일이 허용 크기를 초과합니다."),
    "WAV_FORMAT_UNSUPPORTED": (415, "유효한 PCM WAV 파일을 선택해 주세요."),
    "RECORDING_STORE_FAILED": (503, "저장소에 접근할 수 없습니다. 같은 요청으로 다시 시도해 주세요."),
}


class ApiError(Exception):
    def __init__(self, code):
        # 전역 오류 처리기가 HTML과 JSON 중 요청에 맞는 형식을 고를 때 이 속성들을 사용한다.
        super().__init__(code)
        self.code = code
        self.status, self.message = ERRORS[code]


def payload():
    # JSON 파싱 실패, 빈 본문, 배열 등은 모두 같은 입력 오류로 처리한다.
    # 반환값이 딕셔너리이므로 이후의 뷰는 필드 이름으로 값을 읽을 수 있다.
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("INVALID_INPUT")
    return data


def uuid_field(value):
    # 폼·JSON·쿼리 문자열로 받은 ID를 검증하고 하이픈이 있는 표준 UUID 문자열로 통일한다.
    if not isinstance(value, str):
        raise ApiError("INVALID_INPUT")
    try:
        return str(UUID(value))
    except ValueError:
        raise ApiError("INVALID_INPUT") from None


def text_field(value, limit=4000):
    # 원래 입력 길이로 상한을 검사하고 양끝 공백을 제거한다. 공백만 입력한 값도 거절한다.
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ApiError("INVALID_INPUT")
    return value.strip()


def call_datetime(value):
    # 브라우저가 보낸 시간대가 포함된 ISO 일시를 MySQL DATETIME용 UTC로 바꾼다.
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            raise ValueError
        utc = instant.astimezone(timezone.utc).replace(tzinfo=None)
        if utc.year < 1000:
            raise ValueError
        return utc
    except (TypeError, ValueError, OverflowError):
        raise ApiError("INVALID_INPUT") from None


def digest(value):
    # 입장 코드나 쿠키 토큰을 DB의 이진 해시 열과 비교할 수 있도록 SHA-256 바이트로 변환한다.
    return hashlib.sha256(value.encode()).digest()


def deadline(setting, default):
    # 환경 설정의 단위는 분이다. DB에 기록할 절대 만료 시각을 현재 UTC 기준으로 계산한다.
    return utcnow() + timedelta(minutes=int(os.getenv(setting, default)))


def staff_user(required=True):
    # 쿠키가 가리키는 세션이 만료되지 않았고 연결된 계정도 활성 상태여야 직원으로 인정한다.
    token = request.cookies.get(STAFF_COOKIE)
    user = None
    if token:
        user = staff_queries.session_user(digest(token))
    if user:
        # g는 현재 앱 컨텍스트에 속한다. 이 요청의 감사 로그와 변경 이력이 같은 직원 ID를 사용한다.
        g.actor = user["staff_id"]
    elif required:
        raise ApiError("STAFF_AUTH_REQUIRED")
    # 첫 화면처럼 익명 접근을 허용하는 호출은 required=False로 None을 받아 이동할 화면을 결정한다.
    return user


def customer_session(cookie_name=CHAT_COOKIE, *, lock=False):
    # 호출자가 지정한 쿠키로 아직 유효한 고객 세션을 찾는다. 공개 문의 방문 직후에는
    # 세션 행이 있어도 case_id는 비어 있다. lock=True인 접수 경로는 이 행을 잠가 중복 생성을 막는다.
    token = request.cookies.get(cookie_name)
    if token:
        row = chat_queries.session(digest(token), lock=lock)
        if row and row["case_id"]:
            g.actor = "customer:" + row["case_id"]
        return row
    return None


def admin():
    # 유효한 직원 세션을 확인한 후 관리자 역할을 요구한다. 성공하면 해당 계정 정보를 돌려준다.
    user = staff_user()
    if user["role"] != "ADMIN":
        raise ApiError("CASE_ACCESS_DENIED")
    return user


def agent():
    # 상담원 전용 화면이나 티켓 수락처럼 AGENT 역할이 필요한 처리에서 호출한다.
    user = staff_user()
    if user["role"] != "AGENT":
        raise ApiError("CASE_ACCESS_DENIED")
    return user


def case_row(case_id, lock=False):
    # visible_case는 삭제 표시된 티켓을 제외한다. 이어서 상태를 바꾸는 호출자는
    # FOR UPDATE로 조회해 같은 티켓을 수정하는 다른 트랜잭션과 순서를 맞춘다.
    case = case_queries.by_id(case_id, lock=lock)
    if not case:
        raise ApiError("CASE_NOT_FOUND")
    return case


def assigned_case(case_id, lock=False):
    # 직원 개인 데스크의 상세 조회·쓰기에는 현재 담당 상담원만 접근할 수 있다.
    # DB에서 읽은 현재 배정과 요청 세션의 직원 ID를 대조한다.
    user = staff_user()
    case = case_row(case_id, lock)
    if user["role"] != "AGENT" or case["current_agent_id"] != user["staff_id"]:
        raise ApiError("CASE_ACCESS_DENIED")
    return case


def writable(case):
    # 종결 시각이 있는 티켓에 채팅 추가나 일반 상담 내용 수정이 들어오면 업무 오류를 낸다.
    if case["closed_at"]:
        raise ApiError("CASE_READ_ONLY")


def channel(case, expected):
    # 웹 채팅과 전화 응대가 공유하는 티켓에서, 호출한 기능과 실제 접수 채널이 일치하는지 검사한다.
    if case["channel"] != expected:
        raise ApiError("CHANNEL_MISMATCH")


def status(case):
    # 종료 시각을 우선한다. 열린 티켓은 담당자가 있으면 진행 중, 아직 없으면 대기 상태다.
    if case["closed_at"]:
        return "CLOSED"
    return "IN_PROGRESS" if case["current_agent_id"] else "QUEUED"


def case_info(case):
    # 목록과 상세 응답이 공유하는 티켓 필드만 뽑고, DB의 일시를 API에서 쓰는 UTC 문자열로 바꾼다.
    return {
        "case_id": case["case_id"], "channel": case["channel"], "status": status(case),
        "current_agent_id": case["current_agent_id"], "opened_at": stamp(case["opened_at"]),
        "closed_at": stamp(case["closed_at"]),
        "inquiry_type": case.get("inquiry_type"), "outcome": case.get("outcome"),
    }


def content_visible(case):
    # 기한이 설정되지 않았거나 아직 지나지 않았을 때만 본문을 보여 준다.
    # 정리 작업이 실제 데이터를 삭제하기 전에도 화면과 API에 같은 기한을 적용한다.
    return case["content_expires_at"] is None or case["content_expires_at"] > utcnow()
