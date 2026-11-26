import json
import os
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Blueprint, request

from .cases import partner
from .common import ApiError, payload, text_field, uuid_field
from .db import stamp
from .queries import ai_chat as chats
from .queries import voice

ai_chat = Blueprint("ai_chat", __name__, url_prefix="/partner/ai-chat")


@ai_chat.before_request
def authenticate():
    partner()


def subject_ref():
    return uuid_field(request.headers.get("X-Support-Subject"))


@ai_chat.get("/availability")
def availability():
    configured = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    return {"chat_enabled": bool(chats.policy()["enabled"]), "voice_enabled": bool(voice.policy()["enabled"]),
            "configured": configured}


def conversation(session, policy):
    turns = chats.turns(session["chat_id"]) if session else []
    return {"chat_id": session["chat_id"] if session else None, "enabled": bool(policy["enabled"]),
            "greeting": session["greeting"] if session else policy["greeting"],
            "turns": [{"question": t["question"], "answer": t["answer"], "at": stamp(t["created_at"])} for t in reversed(turns)]}


@ai_chat.get("")
def read():
    subject = subject_ref()
    return conversation(None if request.args.get("new") == "1" else chats.latest(subject), chats.policy())


def generate_reply(history, question, policy):
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ApiError("AI_CHAT_NOT_CONFIGURED")
    instruction = ("RKX 고객센터 AI 상담원입니다. 한국어 존댓말로 서비스 이용과 상담 절차를 안내하세요. "
                   "계정이나 거래를 조회했다고 말하지 말고 담당자 확인이 필요하면 ‘상담원 채팅 문의’를 안내하세요. "
                   "매매 추천은 하지 마세요. ")
    instruction += "번호를 붙여 단계별로 안내하세요." if policy["reply_style"] == "steps" else "핵심만 두세 문장으로 답하세요."
    messages = [{"role": "system", "content": instruction}]
    for turn in reversed(history):
        messages.extend(({"role": "user", "content": turn["question"]}, {"role": "assistant", "content": turn["answer"]}))
    messages.append({"role": "user", "content": question})
    body = {"model": os.environ.get("OPENAI_CHAT_MODEL") or "gpt-4.1-mini", "messages": messages,
            "max_completion_tokens": 500, "store": False}
    req = Request("https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json"})
    req.add_unredirected_header("Authorization", "Bearer " + key)
    try:
        with urlopen(req, timeout=19) as response:
            answer = json.load(response)["choices"][0]["message"]["content"]
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
    if not policy["enabled"]:
        raise ApiError("AI_CHAT_DISABLED")
    answer = generate_reply(chats.turns(chat_id, 10), question, policy)
    chats.add_turn(chat_id, request_id, question, answer)
    return conversation(session, policy), 201
