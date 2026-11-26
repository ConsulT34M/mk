"""격리된 DB에서 관리자 키 교체와 세 OpenAI 호출 경로를 검증한다."""
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

from app import app, openai_settings, voice_summaries
from app.db import one
from app.queries import openai


def check_settings(folder):
    admin, agent, visitor = (app.test_client() for _ in range(3))
    endpoint = "/admin/openai-settings"
    lookup = endpoint + "/chat-models"
    page = "/admin?tab=ai"
    for client, login in ((admin, "admin"), (agent, "agent1")):
        result = client.post("/staff/sessions", json={"login_id": login, "password": os.environ[f"DEMO_{login.upper()}_PASSWORD"]})
        assert result.status_code == 200
    key = "sk-test-" + uuid4().hex
    fields = dict(chat_model="gpt-6.2-sol", voice_model="gpt-realtime-2.1", summary_model="gpt-audio-mini")
    tuned = "ft:gpt-4o-mini-2024-07-18:example::test"
    catalog = [fields["chat_model"], tuned, fields["chat_model"], "gpt-4o-audio-preview", "gpt-realtime",
               "gpt-image-1", "text-embedding-3-small", "gpt-5-pro", "gpt-5-codex", "gpt-4o-mini-transcribe", "o3-deep-research"]
    listed = json.dumps({"data": [{"id": name} for name in catalog]}).encode()
    with app.app_context():
        data = dict(fields, api_key=key, revision=openai.settings()["revision"])
        assert openai_settings.connection("chat_model") == (os.environ["OPENAI_API_KEY"], "server-chat-model")
    assert os.environ["OPENAI_API_KEY"] not in admin.get(page).get_data(as_text=True)
    assert agent.get(page).status_code == 403 and agent.post(endpoint, json=data).status_code == 403
    assert visitor.post(endpoint, json=data).status_code == 401
    assert admin.post(endpoint, json=dict(data, api_key="••••1234")).status_code == 400
    assert admin.post(endpoint, json=dict(data, voice_model=fields["chat_model"])).status_code == 400
    with patch("app.openai_settings.urlopen") as provider:
        assert agent.post(lookup, json={"api_key": key}).status_code == 403
        assert visitor.post(lookup, json={"api_key": key}).status_code == 401
        assert admin.post(lookup, json={"api_key": "bad"}).status_code == 400
        with patch.dict(os.environ, OPENAI_API_KEY=""):
            assert admin.post(lookup, json={}).status_code == 400
        assert fields["chat_model"] not in admin.get(page).get_data(as_text=True)
        provider.assert_not_called()
        for entered, expected in ((key, key), ("", os.environ["OPENAI_API_KEY"])):
            provider.return_value = io.BytesIO(listed)
            result = admin.post(lookup, json={"api_key": entered})
            assert result.json == {"models": sorted([tuned, fields["chat_model"]])}
            req = provider.call_args.args[0]
            assert req.full_url == "https://api.openai.com/v1/models" and req.get_method() == "GET"
            assert req.get_header("Authorization") == "Bearer " + expected and expected.encode() not in result.data
        with app.app_context():
            assert openai.settings()["revision"] == data["revision"] and not openai.settings()["key_ciphertext"]
        provider.side_effect = HTTPError("https://api.openai.com/v1/models", 401, key, {}, None)
        result = admin.post(lookup, json={"api_key": key})
        assert result.status_code == 422 and key.encode() not in result.data
        provider.side_effect = URLError("network")
        assert admin.post(lookup, json={"api_key": key}).status_code == 502
        provider.side_effect = HTTPError("https://api.openai.com/v1/models/missing", 404, "missing", {}, None)
        assert admin.post(endpoint, json=data).status_code == 400
        provider.side_effect = None
        provider.return_value = io.BytesIO(json.dumps({"id": fields["chat_model"]}).encode())
        saved = admin.post(endpoint, json=data)
        req = provider.call_args.args[0]
        assert req.full_url.endswith("/models/" + fields["chat_model"]) and req.get_header("Authorization") == "Bearer " + key
    assert saved.status_code == 200 and saved.json == {"saved": True}
    assert admin.post(endpoint, json=data).status_code == 409
    html = admin.get(page).get_data(as_text=True)
    assert "••••" + key[-4:] in html and key not in html and 'type="password"' in html
    assert fields["chat_model"] in html and Path(folder, ".openai-settings.key").is_file()
    with patch("app.openai_settings.urlopen", return_value=io.BytesIO(listed)) as provider:
        assert admin.post(lookup, json={}).status_code == 200
        assert provider.call_args.args[0].get_header("Authorization") == "Bearer " + key
    with app.app_context():
        row = openai.settings()
        assert row["key_ciphertext"] and key.encode() not in row["key_ciphertext"]
        assert all(openai_settings.connection(field) == (key, model) for field, model in fields.items())
    headers = {"Authorization": "Bearer " + os.environ["PARTNER_TOKEN"], "X-Support-Subject": str(uuid4())}
    with patch.dict(os.environ, OPENAI_API_KEY=""):
        assert visitor.get("/partner/ai-chat/availability", headers=headers).json["configured"]
        assert visitor.get("/inquiry").status_code == 200

    def completion(req, expected_model):
        assert req.get_header("Authorization") == "Bearer " + key
        assert json.loads(req.data)["model"] == expected_model
        content = {"choices": [{"finish_reason": "stop", "message": {"content": "상담 요청"}}]}
        return io.BytesIO(json.dumps(content).encode())

    with patch("app.ai_chat.urlopen", side_effect=lambda req, timeout: completion(req, fields["chat_model"])):
        result = visitor.post("/partner/ai-chat/messages", headers=headers,
                              json={"chat_id": str(uuid4()), "request_id": str(uuid4()), "body": "상담 문의"})
        assert result.status_code == 201 and result.json["turns"][0]["answer"] == "상담 요청"
    base = "/inquiry/voice/sessions"
    with patch("app.voice.provider_request", return_value=(b"v=0", "/v1/realtime/calls/test-call")) as provider, \
            patch("app.voice.watch_call") as listener:
        result = visitor.post(base, json={"sdp": "v=0", "recording_consent": True, "summary_consent": True})
        assert result.status_code == 201 and key.encode() not in result.data
        req, call_key = provider.call_args.args
        assert call_key == key and json.dumps(fields["voice_model"]).encode() in req.data
        assert listener.call_args.args[-2:] == ("test-call", key)
    voice_id = result.json["voice_id"]
    session = f"{base}/{voice_id}"
    assert visitor.post(session + "/recording/0", data=bytes(3200), content_type="application/octet-stream").status_code == 204
    assert visitor.post(session + "/end", json={}).json == {"recorded": True}
    with app.app_context(), patch("app.voice_summaries.urlopen", side_effect=lambda req, timeout: completion(req, fields["summary_model"])):
        assert voice_summaries.process_next_summary()
        assert one("SELECT state FROM ai_call_summary WHERE recording_id=%s", (voice_id,))["state"] == "READY"

    data.update(revision=row["revision"], api_key="", chat_model="")
    assert admin.post(endpoint, json=data).status_code == 200
    with app.app_context():
        assert openai_settings.connection("chat_model") == (key, "server-chat-model")
        data["revision"] = openai.settings()["revision"]
    replacement = "sk-test-" + uuid4().hex
    assert admin.post(endpoint, data=dict(data, api_key=replacement)).status_code == 303
    with app.app_context():
        assert openai_settings.connection("voice_model") == (replacement, fields["voice_model"])
    html = admin.get(page).get_data(as_text=True)
    assert replacement not in html and "••••" + replacement[-4:] in html


if __name__ == "__main__":
    with TemporaryDirectory() as folder, patch.dict(app.config, RECORDING_DIR=folder), \
            patch.dict(os.environ, OPENAI_API_KEY="sk-server-test-only", OPENAI_CHAT_MODEL="server-chat-model"):
        check_settings(folder)
    print("PASS admin-only encrypted key, rotation, environment defaults and model routing for chat, calls and summaries")
