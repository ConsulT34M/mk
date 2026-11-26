"""격리된 MK 테스트 DB에서 RKX 상담 채널을 검증한다."""

import os
from uuid import uuid4
from unittest.mock import patch

from app import app
from app.common import ApiError
from app.db import one


def check_channels():
    customer, admin = app.test_client(), app.test_client()
    headers = {"Authorization": "Bearer " + os.environ["PARTNER_TOKEN"], "X-Support-Subject": str(uuid4())}
    other = dict(headers, **{"X-Support-Subject": str(uuid4())})
    path = "/partner/ai-chat"
    assert admin.post("/staff/sessions", json={"login_id": "admin", "password": os.environ["DEMO_ADMIN_PASSWORD"]}).status_code == 200
    assert customer.get(path, headers={"X-Support-Subject": headers["X-Support-Subject"]}).status_code == 401
    policy = {"enabled": True, "greeting": "무엇이 궁금하세요?", "reply_style": "steps"}
    assert admin.post("/admin/ai-chat-settings", json=policy).status_code == 200
    assert customer.get(path, headers=headers).json["greeting"] == policy["greeting"]
    data = {"chat_id": str(uuid4()), "request_id": str(uuid4()), "body": "상담 메뉴를 알려 주세요."}
    def send(message, owner=headers):
        return customer.post(path + "/messages", headers=owner, json=message)

    with patch("app.ai_chat.generate_reply", return_value="상담 버튼을 누르세요.") as model:
        result = send(data)
        assert result.status_code == 201 and result.json["turns"][0]["answer"] == model.return_value
        assert model.call_args.args[2]["reply_style"] == "steps"
        assert send(data).status_code == 200 and model.call_count == 1
        assert send(dict(data, body="다른 질문")).status_code == 409
        assert send(data, other).status_code == 403
    assert not customer.get(path, headers=other).json["turns"]
    history = admin.get("/admin", query_string={"tab": "ai-chats", "chat": data["chat_id"]}).get_data(as_text=True)
    assert data["body"] in history and "상담 버튼을 누르세요." in history
    blank = dict(data, chat_id=str(uuid4()))
    with patch("app.ai_chat.generate_reply", side_effect=ApiError("AI_CHAT_UNAVAILABLE")):
        assert send(blank).status_code == 502
    assert admin.post("/admin/ai-chat-settings", json=dict(policy, enabled=False)).status_code == 200
    assert not customer.get(path + "/availability", headers=headers).json["chat_enabled"]
    assert send(blank).json["code"] == "AI_CHAT_DISABLED"
    with app.app_context():
        assert not one("SELECT 1 FROM ai_chat_session WHERE chat_id=%s", (blank["chat_id"],))
        assert not one("SELECT 1 FROM support_case WHERE support_subject_ref=%s", (headers["X-Support-Subject"],))

    with patch("app.voice.client_secret", return_value="temporary"):
        start = customer.post("/inquiry/voice/sessions", headers=headers, json={})
    assert start.status_code == 201
    voice = "/inquiry/voice/sessions/" + start.json["voice_id"]
    assert customer.post(voice + "/connected", headers=other, json={}).status_code == 404
    assert customer.post(voice + "/connected", headers=headers, json={}).status_code == 204
    assert customer.post(voice + "/recording/0", headers=headers, data=b"\0\0",
                         content_type="application/octet-stream").json["code"] == "RECORDING_CONSENT_REQUIRED"
    assert customer.post(voice + "/handoff", headers=other, json={}).status_code == 404
    assert customer.post(voice + "/handoff", headers=headers, json={}).status_code == 201
    assert customer.post(voice + "/handoff", headers=headers, json={}).status_code == 200
    assert customer.post(voice + "/end", headers=headers, json={}).status_code == 204
    with app.app_context():
        log = one("SELECT * FROM rkx_voice_call WHERE voice_id=%s", (start.json["voice_id"],))
        assert log["subject_ref"] == headers["X-Support-Subject"] and log["connected_at"] and log["ended_at"]
        assert not one("SELECT 1 FROM recording WHERE recording_id=%s", (start.json["voice_id"],))


if __name__ == "__main__":
    check_channels()
    print("PASS RKX AI settings, conversation history, replay, customer isolation, voice consent and handoff")
