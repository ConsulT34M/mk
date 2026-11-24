import json
import os
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Blueprint, request

from .cases import intake_case
from .common import INQUIRY_COOKIE, ApiError, customer_session, digest, payload
from .db import audit, new_id, one, run
from .voice_recording import append_voice, finalize_voice, reserve_voice

voice = Blueprint("voice", __name__, url_prefix="/inquiry/voice")


def client_secret():
    policy = one("SELECT enabled,instructions,offer_handoff FROM voice_policy WHERE policy_id=1")
    if not policy["enabled"]:
        raise ApiError("VOICE_DISABLED")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ApiError("VOICE_NOT_CONFIGURED")
    session = {
        "type": "realtime",
        "model": os.environ.get("OPENAI_REALTIME_MODEL") or "gpt-realtime-2.1-mini",
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
    session["instructions"] += (" 해결하기 어려운 문의는 상담원 접수를 제안하고 동의 후 인계하세요."
                                if policy["offer_handoff"] else " 고객이 직접 요청한 경우에만 상담원 접수를 진행하세요.")
    if policy["instructions"]:
        session["instructions"] += "\n운영 응대 지침\n" + policy["instructions"]
    body = {"expires_after": {"anchor": "created_at", "seconds": 60}, "session": session}
    req = Request("https://api.openai.com/v1/realtime/client_secrets",
                  data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    req.add_unredirected_header("Authorization", "Bearer " + key)
    try:
        with urlopen(req, timeout=12) as response:
            result = json.load(response)
    except HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise ApiError("VOICE_AUTH_FAILED") from None
        if error.code == 429:
            raise ApiError("VOICE_LIMIT_REACHED") from None
        raise ApiError("VOICE_UNAVAILABLE") from None
    except (URLError, OSError, HTTPException, ValueError):
        raise ApiError("VOICE_UNAVAILABLE") from None
    if not isinstance(result, dict) or not isinstance(result.get("value"), str) or not result["value"]:
        raise ApiError("VOICE_UNAVAILABLE")
    return result["value"]


def visitor_hash(*, lock=False):
    if not customer_session(INQUIRY_COOKIE, lock=lock):
        raise ApiError("VOICE_VISITOR_EXPIRED")
    return digest(request.cookies[INQUIRY_COOKIE])


@voice.post("/sessions")
def start():
    consent = payload().get("recording_consent", False)
    if not isinstance(consent, bool):
        raise ApiError("INVALID_INPUT")
    visitor_hash()
    secret = client_secret()
    # Only bind a successful upstream session, without locking the visitor during the network request.
    owner = visitor_hash(lock=True)
    voice_id = new_id()
    run("INSERT INTO voice_session (voice_id,owner_hash,subject_ref) VALUES (%s,%s,%s)",
        (voice_id, owner, new_id()))
    if consent:
        reserve_voice(voice_id)
    audit("VOICE_RECORDING_AGREED" if consent else "VOICE_RECORDING_REFUSED", "customer", recording_id=voice_id)
    return {"voice_id": voice_id, "client_secret": secret}, 201


@voice.post("/sessions/<uuid:voice_id>/handoff")
def handoff(voice_id):
    payload()
    owner = visitor_hash(lock=True)
    session = one("SELECT subject_ref FROM voice_session WHERE voice_id=%s AND owner_hash=%s", (voice_id, owner))
    if not session:
        raise ApiError("VOICE_SESSION_NOT_FOUND")
    # One intake key per conversation, even when the model repeats the tool call.
    case_id, created = intake_case(session["subject_ref"], voice_id, "PHONE")
    if created:
        run("UPDATE recording SET case_id=%s WHERE recording_id=%s AND uploaded_by IS NULL", (case_id, voice_id))
    return {"case_id": case_id}, 201 if created else 200


def owned_recording(voice_id):
    owner = visitor_hash(lock=True)
    if not one("SELECT 1 FROM voice_session WHERE voice_id=%s AND owner_hash=%s", (voice_id, owner)):
        raise ApiError("VOICE_SESSION_NOT_FOUND")
    return one("SELECT * FROM recording WHERE recording_id=%s AND uploaded_by IS NULL FOR UPDATE", (voice_id,))


@voice.post("/sessions/<uuid:voice_id>/recording/<int:offset>")
def receive_audio(voice_id, offset):
    if request.mimetype != "application/octet-stream":
        raise ApiError("INVALID_INPUT")
    data = request.get_data()
    item = owned_recording(voice_id)
    if not item:
        raise ApiError("RECORDING_CONSENT_REQUIRED")
    try:
        append_voice(item, offset, data)
    except OSError:
        raise ApiError("RECORDING_STORE_FAILED") from None
    return "", 204


@voice.post("/sessions/<uuid:voice_id>/end")
def end(voice_id):
    payload()
    try:
        item = owned_recording(voice_id)
    except ApiError as error:
        if error.code == "VOICE_SESSION_NOT_FOUND":
            return "", 204
        raise
    run("DELETE FROM voice_session WHERE voice_id=%s", (voice_id,))
    try:
        saved = finalize_voice(item) if item else False
    except OSError:
        raise ApiError("RECORDING_STORE_FAILED") from None
    return {"recorded": saved} if item else ("", 204)
