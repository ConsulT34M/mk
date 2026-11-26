import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Blueprint, request

from . import openai_settings
from .ai_usage import record_usage
from .cases import intake_case, partner
from .common import ApiError, case_row, payload, text_field, uuid_field
from .db import stamp
from .queries import ai_chat as chats
from .queries import ai_outcomes, voice

ai_chat = Blueprint("ai_chat", __name__, url_prefix="/partner/ai-chat")


@ai_chat.before_request
def authenticate():
    partner()


def subject_ref():
    return uuid_field(request.headers.get("X-Support-Subject"))


@ai_chat.get("/availability")
def availability():
    return {"chat_enabled": bool(chats.policy()["enabled"]), "voice_enabled": bool(voice.policy()["enabled"]),
            "configured": openai_settings.configured()}


def conversation(session, policy):
    turns = chats.turns(session["chat_id"]) if session else []
    result = ai_outcomes.get("WEB", session["chat_id"]) if session else None
    return {"chat_id": session["chat_id"] if session else None, "enabled": bool(policy["enabled"]),
            "outcome": result["outcome"] if result else None,
            "greeting": session["greeting"] if session else policy["greeting"],
            "turns": [{"question": t["question"], "answer": t["answer"], "at": stamp(t["created_at"])} for t in reversed(turns)]}


@ai_chat.get("")
def read():
    subject = subject_ref()
    return conversation(None if request.args.get("new") == "1" else chats.latest(subject), chats.policy())


@ai_chat.post("/<uuid:chat_id>/resolve")
@ai_chat.post("/<uuid:chat_id>/handoff", defaults={"handoff": True})
def finish(chat_id, handoff=False):
    data = payload()
    subject = subject_ref()
    if not chats.owned_for_update(chat_id, subject):
        raise ApiError("CASE_ACCESS_DENIED")
    previous = ai_outcomes.get("WEB", chat_id)
    outcome = "HANDOFF" if handoff else "RESOLVED"
    if previous and previous["outcome"] != outcome:
        raise ApiError("AI_CONSULTATION_FINISHED")
    if not handoff:
        if not previous:
            ai_outcomes.add("WEB", chat_id, outcome)
        return {"outcome": outcome}
    if previous:
        case = case_row(previous["case_id"])
        return {"case_id": case["case_id"], "intake_request_id": case["intake_request_id"]}
    intake = uuid_field(data.get("intake_request_id"))
    try:
        case_id, created = intake_case(subject, intake, "WEB")
    except ApiError as error:
        if error.code == "TICKET_DELETED":
            raise ApiError("AI_HANDOFF_ROOM_CLOSED") from None
        raise
    if not created and case_row(case_id)["closed_at"]:
        raise ApiError("AI_HANDOFF_ROOM_CLOSED")
    ai_outcomes.add("WEB", chat_id, outcome, case_id)
    return {"case_id": case_id, "intake_request_id": intake}, 201 if created else 200


def generate_reply(history, question, policy):
    key, model = openai_settings.connection("chat_model")
    if not key:
        raise ApiError("AI_CHAT_NOT_CONFIGURED")
    instruction = ("RKX 고객센터 AI 상담원입니다. 한국어 존댓말로 서비스 이용과 상담 절차를 안내하세요. "
                   "계정이나 거래를 조회했다고 말하지 말고 담당자 확인이 필요하면 채팅창의 ‘상담원 연결’ 버튼을 안내하세요. "
                   "매매 추천은 하지 마세요. ")
    instruction += "번호를 붙여 단계별로 안내하세요." if policy["reply_style"] == "steps" else "핵심만 두세 문장으로 답하세요."
    messages = [{"role": "system", "content": instruction}]
    for turn in reversed(history):
        messages.extend(({"role": "user", "content": turn["question"]}, {"role": "assistant", "content": turn["answer"]}))
    messages.append({"role": "user", "content": question})
    body = {"model": model, "messages": messages,
            "max_completion_tokens": 500, "store": False}
    req = Request("https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json"})
    req.add_unredirected_header("Authorization", "Bearer " + key)
    try:
        with urlopen(req, timeout=19) as response:
            result = json.load(response)
        record_usage("CHAT", result)
        answer = result["choices"][0]["message"]["content"]
        if isinstance(answer, str) and answer.strip():
            return answer.strip()
    except HTTPError as error:
        error.close()
        if error.code == 429:
            raise ApiError("AI_CHAT_LIMIT_REACHED") from None
    except (OSError, URLError, HTTPException, ValueError, KeyError, IndexError, TypeError):
        pass
    raise ApiError("AI_CHAT_UNAVAILABLE")


@ai_chat.post("/messages")
def send():
    data = payload()
    subject, chat_id = subject_ref(), uuid_field(data.get("chat_id"))
    request_id, question = uuid_field(data.get("request_id")), text_field(data.get("body"), 2000)
    policy = chats.policy()
    # 같은 대화의 전송을 직렬화해 재전송 시 답변을 다시 생성하지 않는다.
    session = chats.lock_session(chat_id, subject, policy["greeting"])
    if not session:
        raise ApiError("CASE_ACCESS_DENIED")
    prior = chats.prior_turn(chat_id, request_id)
    if prior:
        if prior["question"] != question:
            raise ApiError("REQUEST_KEY_CONFLICT")
        return conversation(session, policy)
    if ai_outcomes.get("WEB", chat_id):
        raise ApiError("AI_CONSULTATION_FINISHED")
    if not policy["enabled"]:
        raise ApiError("AI_CHAT_DISABLED")
    answer = generate_reply(chats.turns(chat_id, 10), question, policy)
    chats.add_turn(chat_id, request_id, question, answer)
    return conversation(session, policy), 201
