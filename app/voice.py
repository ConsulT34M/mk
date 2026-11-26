import hmac
import json
import os
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from flask import Blueprint, current_app, request

from . import openai_settings
from .cases import intake_case, partner
from .common import INQUIRY_COOKIE, ApiError, call_datetime, customer_session, deadline, digest, payload, uuid_field
from .db import audit, new_id
from .queries import ai_outcomes
from .queries import cases as case_queries
from .queries import chat as chat_queries
from .queries import recordings as recording_queries
from .queries import voice as voice_queries
from .realtime_usage import watch_call
from .voice_recording import append_voice, finalize_voice, reserve_voice

# 공개 문의 화면의 음성 상담 API다. 방문 쿠키로 통화의 소유자를 확인하고 voice_id로
# 상담원 인계, 녹취 조각 수신, 통화 종료 요청을 같은 대화에 연결한다.
voice = Blueprint("voice", __name__, url_prefix="/inquiry/voice")


@voice.get("/ringback")
def ringback():
    policy = voice_queries.policy()
    if policy["enabled"] and policy["ringback_enabled"]:
        return current_app.send_static_file("voice-ringback.mp3")
    return "", 204


def call_settings(policy):
    # 운영 설정과 서버 키를 확인한 뒤 브라우저가 음성 서비스에 연결할 단기 자격 증명을 요청한다.
    # 모델·목소리·상담 원칙과 상담원 접수 도구의 형식은 서버에서 결정한다.
    if not policy["enabled"]:
        raise ApiError("VOICE_DISABLED")
    key, model = openai_settings.connection("voice_model")
    if not key:
        raise ApiError("VOICE_NOT_CONFIGURED")
    session = {
        "type": "realtime",
        "model": model,
        "audio": {"output": {"voice": "marin"}},
        "instructions": (
            "말결 TM 고객센터의 AI 상담원으로 한국어 존댓말로 짧게 대화하세요. "
            "고객센터에서는 채팅 문의를 남기거나 상담원 응대를 요청할 수 있습니다. "
            "질문이 불분명하면 한 가지를 되물으세요. 계정과 거래 정보 조회는 상담원에게 맡기세요. "
            "고객이 상담원을 요청하면 request_agent 도구를 호출하세요. 도구가 성공한 뒤에만 접수 사실을 알리세요. "
            "이 체험에서 인계 결과는 대기 티켓 발행입니다. 실제 전화 연결이나 연락 시각을 약속하지 마세요."
        ),
        "tools": [{
            "type": "function", "name": "request_agent",
            "description": "고객이 상담원 응대를 요청하거나 제안에 동의했을 때 대기 티켓 한 건을 접수합니다.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }],
        "tool_choice": "auto",
    }
    # 자동 인계 제안을 할지 운영 설정으로 정하고, 관리자가 입력한 추가 응대 지침도 붙인다.
    session["instructions"] += (" 해결하기 어려운 문의는 상담원 접수를 제안하고 동의 후 인계하세요."
                                if policy["offer_handoff"] else " 고객이 직접 요청한 경우에만 상담원 접수를 진행하세요.")
    if policy["instructions"]:
        session["instructions"] += "\n운영 응대 지침\n" + policy["instructions"]
    return key, session


def provider_request(req, key):
    req.add_unredirected_header("Authorization", "Bearer " + key)
    try:
        with urlopen(req, timeout=12) as response:
            return response.read(), getattr(response, "headers", {}).get("Location", "")
    except HTTPError as error:
        # 외부 서비스의 인증·권한 오류, 사용량 제한, 그 밖의 장애를 화면이 처리할 업무 코드로 변환한다.
        error.close()
        if error.code in (401, 403):
            raise ApiError("VOICE_AUTH_FAILED") from None
        if error.code == 429:
            raise ApiError("VOICE_LIMIT_REACHED") from None
        raise ApiError("VOICE_UNAVAILABLE") from None
    except (URLError, OSError, HTTPException, ValueError):
        raise ApiError("VOICE_UNAVAILABLE") from None


def client_secret(policy):
    # 이전 클라이언트용 단기 키 발급 경로.
    key, session = call_settings(policy)
    body = {"expires_after": {"anchor": "created_at", "seconds": 60}, "session": session}
    req = Request("https://api.openai.com/v1/realtime/client_secrets",
                  data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    raw, _ = provider_request(req, key)
    try:
        result = json.loads(raw)
    except ValueError:
        raise ApiError("VOICE_UNAVAILABLE") from None
    if not isinstance(result, dict) or not isinstance(result.get("value"), str) or not result["value"]:
        raise ApiError("VOICE_UNAVAILABLE")
    return result["value"]


def create_browser_call(policy, offer):
    key, session = call_settings(policy)
    boundary = new_id()
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="sdp"\r\nContent-Type: application/sdp\r\n\r\n'
            f'{offer}\r\n--{boundary}\r\nContent-Disposition: form-data; name="session"\r\n'
            f'Content-Type: application/json\r\n\r\n{json.dumps(session)}\r\n--{boundary}--\r\n')
    req = Request("https://api.openai.com/v1/realtime/calls", data=body.encode(),
                  headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    raw, location = provider_request(req, key)
    call_id = urlsplit(location).path.rsplit("/", 1)[-1]
    if not raw.startswith(b"v=0") or not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", call_id):
        raise ApiError("VOICE_UNAVAILABLE")
    return raw.decode(), call_id, key


def rkx_subject():
    if "X-Support-Subject" in request.headers or "Authorization" in request.headers:
        partner()
        return uuid_field(request.headers.get("X-Support-Subject"))


def visitor_hash(*, lock=False, start=False):
    subject = rkx_subject()
    if subject:
        owner = hmac.digest(os.environ["PARTNER_TOKEN"].encode(), ("rkx-voice:" + subject).encode(), "sha256")
        if start:
            voice_queries.extend_rkx_session(owner, deadline("CHAT_SESSION_MINUTES", 60))
        if lock and not chat_queries.session(owner, lock=True):
            raise ApiError("VOICE_SESSION_NOT_FOUND")
        return owner
    # 유효한 문의 방문 세션인지 먼저 확인하고, 통화 소유자와 대조할 쿠키 해시를 반환한다.
    # 세션을 연결하거나 수정하는 요청은 방문 세션 행도 잠글 수 있다.
    if not customer_session(INQUIRY_COOKIE, lock=lock):
        raise ApiError("VOICE_VISITOR_EXPIRED")
    return digest(request.cookies[INQUIRY_COOKIE])


@voice.post("/sessions")
def start():
    # 녹취 동의와 요약 동의는 별개로 받는다. 요약은 녹음된 파일로 생성하므로 녹취 동의가 전제다.
    data = payload()
    consent = data.get("recording_consent", False)
    summary_consent = data.get("summary_consent", False)
    if not isinstance(consent, bool) or not isinstance(summary_consent, bool) or (summary_consent and not consent):
        raise ApiError("INVALID_INPUT")
    visitor_hash()
    policy = voice_queries.policy()
    call_id = None
    if "sdp" in data:
        offer = data["sdp"]
        if not isinstance(offer, str) or not offer.startswith("v=0") or len(offer) > 64000:
            raise ApiError("INVALID_INPUT")
        answer, call_id, call_key = create_browser_call(policy, offer)
        access = {"sdp": answer}
    else:
        access = {"client_secret": client_secret(policy)}
    # 외부 서비스 응답을 기다리는 동안에는 방문 세션을 잠그지 않는다. 발급을 마친 뒤
    # 방문 세션을 다시 확인하고 잠가 이 브라우저와 새 음성 세션을 연결한다.
    owner = visitor_hash(lock=True, start=True)
    voice_id = new_id()
    subject = rkx_subject()
    voice_queries.create_session(voice_id, owner, subject or new_id())
    if subject:
        voice_queries.log_rkx_start(voice_id, subject)
    if consent:
        # 동의한 경우만 녹취 행을 예약한다. 요약에도 동의했으면 작업을 미리 등록해 두고
        # 실제 요약은 녹취가 READY가 된 뒤 별도 작업자가 실행한다.
        reserve_voice(voice_id)
        if summary_consent:
            voice_queries.add_summary(voice_id)
    audit("VOICE_RECORDING_AGREED" if consent else "VOICE_RECORDING_REFUSED", "customer", recording_id=voice_id)
    if call_id:
        watch_call(current_app._get_current_object(), voice_id, call_id, call_key)
    return {"voice_id": voice_id, "greeting": policy["greeting"], **access}, 201


@voice.post("/sessions/<uuid:voice_id>/connected")
def connected(voice_id):
    if not voice_queries.owned_session(voice_id, visitor_hash(lock=True)):
        raise ApiError("VOICE_SESSION_NOT_FOUND")
    voice_queries.log_rkx_connected(voice_id)
    return "", 204


@voice.post("/sessions/<uuid:voice_id>/handoff")
def handoff(voice_id):
    # 문의 쿠키의 소유자와 통화 ID가 함께 일치해야 상담원 대기 티켓을 접수할 수 있다.
    data = payload()
    owner = visitor_hash(lock=True)
    session = voice_queries.owned_session(voice_id, owner)
    if not session:
        raise ApiError("VOICE_SESSION_NOT_FOUND")
    previous = ai_outcomes.get("PHONE", voice_id)
    if previous and previous["outcome"] == "RESOLVED":
        raise ApiError("AI_CONSULTATION_FINISHED")
    # 음성 세션 ID 자체를 접수 요청 키로 쓴다. 같은 통화에서 인계 도구를 다시 호출해도
    # 최초 전화 티켓을 반환한다. 최초 접수 때 이미 예약된 AI 녹취가 있으면 그 티켓에 연결한다.
    call_at = call_datetime(data["call_at"]) if "call_at" in data else None
    case_id, created = intake_case(session["subject_ref"], voice_id, "PHONE", call_at=call_at)
    if created:
        recording_queries.link_voice_case(voice_id, case_id)
    if not previous:
        ai_outcomes.add("PHONE", voice_id, "HANDOFF", case_id)
    return {"case_id": case_id}, 201 if created else 200


@voice.post("/sessions/<uuid:voice_id>/resolve")
def resolve(voice_id):
    payload()
    if not voice_queries.owned_session(voice_id, visitor_hash(lock=True)):
        raise ApiError("VOICE_SESSION_NOT_FOUND")
    previous = ai_outcomes.get("PHONE", voice_id)
    if (previous and previous["outcome"] == "HANDOFF") or case_queries.by_intake(voice_id):
        raise ApiError("AI_CONSULTATION_FINISHED")
    if not previous:
        ai_outcomes.add("PHONE", voice_id, "RESOLVED")
    return {"outcome": "RESOLVED"}


def owned_recording(voice_id):
    # 방문자와 통화의 소유 관계를 먼저 확인한 뒤 AI 녹취 행을 잠근다.
    # 유효한 통화여도 녹취에 동의하지 않았다면 예약 행이 없어 None을 반환한다.
    owner = visitor_hash(lock=True)
    if not voice_queries.owned_session(voice_id, owner):
        raise ApiError("VOICE_SESSION_NOT_FOUND")
    return recording_queries.lock_voice_recording(voice_id)


@voice.post("/sessions/<uuid:voice_id>/recording/<int:offset>")
def receive_audio(voice_id, offset):
    # 브라우저는 WAV 헤더가 없는 PCM 조각을 보낸다. URL의 offset은 음성 데이터 안에서
    # 이 조각이 시작하는 바이트 위치다. append_voice가 이를 기준으로 순서와 중복을 확인한다.
    if request.mimetype != "application/octet-stream":
        raise ApiError("INVALID_INPUT")
    data = request.get_data()
    item = owned_recording(voice_id)
    if not item:
        raise ApiError("RECORDING_CONSENT_REQUIRED")
    started_at = None
    if offset == 0 and "X-Call-Started-At" in request.headers:
        started_at = call_datetime(request.headers["X-Call-Started-At"])
    try:
        append_voice(item, offset, data, started_at)
    except OSError:
        raise ApiError("RECORDING_STORE_FAILED") from None
    return "", 204


@voice.post("/sessions/<uuid:voice_id>/end")
def end(voice_id):
    # 통화 소유권을 확인하고 세션을 닫는다. 일치하는 세션이 이미 없으면 종료 재요청에도 204로 응답한다.
    payload()
    try:
        item = owned_recording(voice_id)
    except ApiError as error:
        if error.code == "VOICE_SESSION_NOT_FOUND":
            return "", 204
        raise
    voice_queries.end_session(voice_id)
    if rkx_subject():
        voice_queries.log_rkx_end(voice_id)
    # 녹취에 동의한 통화는 수신한 음성을 WAV로 확정하고 실제 저장 여부를 JSON으로 알려 준다.
    # 녹취 예약이 없는 통화는 응답 본문 없이 종료한다.
    try:
        saved = finalize_voice(item) if item else False
    except OSError:
        raise ApiError("RECORDING_STORE_FAILED") from None
    return {"recorded": saved} if item else ("", 204)
