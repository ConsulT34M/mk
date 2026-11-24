"""Run inside the disposable app container with MySQL. OpenAI is mocked throughout."""
import io
import json
import os
import wave
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from unittest.mock import patch
from urllib.error import HTTPError

from app import app, recording_store
from app.common import INQUIRY_COOKIE
from app.db import db, one, run
from app.recordings import purge
from app.voice_recording import partial_path


def test_voice_handoff():
    path = "/inquiry/voice/sessions"
    visitor, outsider = app.test_client(), app.test_client()
    provider = import_module("app.voice")
    with patch.object(provider, "urlopen") as upstream, patch.dict(os.environ, OPENAI_API_KEY=""):
        assert visitor.post(path, json={}).status_code == 401
        assert visitor.get("/inquiry").status_code == 200
        assert visitor.post(path, json={}).json["code"] == "VOICE_NOT_CONFIGURED"
        upstream.assert_not_called()
        os.environ["OPENAI_API_KEY"] = "unit-test-only"
        for status, code in [(401, "VOICE_AUTH_FAILED"), (429, "VOICE_LIMIT_REACHED")]:
            upstream.side_effect = HTTPError("", status, "", {}, io.BytesIO())
            assert visitor.post(path, json={}).json["code"] == code
        upstream.side_effect = None
        upstream.return_value = io.BytesIO(b"{}")
        assert visitor.post(path, json={}).json["code"] == "VOICE_UNAVAILABLE"
        upstream.return_value = io.BytesIO(b'{"value":"temporary-test-secret"}')
        started = visitor.post(path, json={})
        assert started.status_code == 201 and started.json["client_secret"] == "temporary-test-secret"
        req = upstream.call_args.args[0]
        assert req.full_url == "https://api.openai.com/v1/realtime/client_secrets"
        assert json.loads(req.data)["session"]["tools"][0]["name"] == "request_agent"

    voice_id = started.json["voice_id"]
    session = path + "/" + voice_id
    handoff = session + "/handoff"
    with app.app_context():
        assert not one("SELECT case_id FROM support_case WHERE intake_request_id=%s", (voice_id,))
    outsider.get("/inquiry")
    assert outsider.post(handoff, json={}).status_code == 404
    assert outsider.post(session + "/end", json={}).status_code == 204
    assert visitor.post(handoff, json={}, headers={"Origin": "https://example.invalid"}).status_code == 403

    def request_handoff(_):
        tab = app.test_client()
        tab.set_cookie(INQUIRY_COOKIE, visitor.get_cookie(INQUIRY_COOKIE).value)
        return tab.post(handoff, json={})

    with ThreadPoolExecutor(2) as pool:
        attempts = list(pool.map(request_handoff, range(2)))
    assert sorted(result.status_code for result in attempts) == [200, 201]
    ticket = attempts[0].json["case_id"]
    assert attempts[1].json["case_id"] == ticket
    with app.app_context():
        case = one("SELECT channel,current_agent_id FROM visible_case WHERE case_id=%s", (ticket,))
        assert case == {"channel": "PHONE", "current_agent_id": None}
        assert one("SELECT case_id FROM phone_response WHERE case_id=%s", (ticket,))
    assert visitor.post(session + "/end", json={}).status_code == 204
    assert visitor.post(session + "/end", json={}).status_code == 204
    assert visitor.post(handoff, json={}).status_code == 404


def test_voice_recordings():
    visitor, other, admin, agent = [app.test_client() for _ in range(4)]
    for customer in (visitor, other):
        customer.get("/inquiry")
    for client, login, password in [(admin, "admin", os.environ["DEMO_ADMIN_PASSWORD"]), (agent, "test", "test")]:
        assert client.post("/staff/sessions", json={"login_id": login, "password": password}).status_code == 200
    base = "/inquiry/voice/sessions"
    pcm = bytes(range(32)) * 256

    def start(consent):
        with patch.object(import_module("app.voice"), "urlopen", return_value=io.BytesIO(b'{"value":"test"}')), \
                patch.dict(os.environ, OPENAI_API_KEY="fixture-only"):
            result = visitor.post(base, json={"recording_consent": consent})
        assert result.status_code == 201
        return result.json["voice_id"]

    def push(client, rid, offset=0, data=pcm):
        return client.post(f"{base}/{rid}/recording/{offset}", data=data, content_type="application/octet-stream")

    refused = start(False)
    assert push(visitor, refused).json["code"] == "RECORDING_CONSENT_REQUIRED"
    assert visitor.post(f"{base}/{refused}/end", json={}).status_code == 204
    for handoff in (False, True):
        rid = start(True)
        session = f"{base}/{rid}"
        clip = f"/admin/voice-recordings/{rid}/audio"
        assert push(other, rid).status_code == 404
        assert push(visitor, rid, 2).status_code == 409
        assert push(visitor, rid).status_code == push(visitor, rid).status_code == 204
        assert push(visitor, rid, data=b"xxxx").status_code == 409
        assert push(visitor, rid, len(pcm)).status_code == 204
        assert admin.get(clip).status_code == 409 and agent.get(clip).status_code == 403
        if handoff:
            ticket = visitor.post(session + "/handoff", json={}).json["case_id"]
        with patch.object(recording_store, "put_audio", side_effect=OSError("fixture")):
            assert visitor.post(session + "/end", json={}).status_code == 503
        assert visitor.post(session + "/end", json={}).json == {"recorded": True}
        assert visitor.post(session + "/end", json={}).status_code == 204
        label = "AI 상담 " + rid[:8]
        assert admin.post(clip[:-6], json={"label": label}).status_code == 200
        listing = admin.get("/admin?tab=voice-recordings").data
        assert clip.encode() in listing and label.encode() in listing
        with admin.get(clip) as response:
            assert response.status_code == 200 and response.mimetype == "audio/wav"
            with wave.open(io.BytesIO(response.data)) as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
                assert wav.readframes(wav.getnframes()) == pcm + pcm
        with app.app_context():
            item = one("SELECT case_id,object_key FROM recording WHERE recording_id=%s", (rid,))
            assert item["object_key"] == "files/" + rid + ".wav" and not partial_path(rid).exists()
            assert one("SELECT event_id FROM audit_event WHERE action='RECORDING_PLAY' AND recording_id=%s "
                       "AND result='OK' AND actor=(SELECT staff_id FROM staff_account WHERE login_id='admin')", (rid,))
            if not handoff:
                assert item["case_id"] is None and not one("SELECT 1 FROM support_case WHERE intake_request_id=%s", (rid,))
        if handoff:
            assert item["case_id"] == ticket
            assert label.encode() in admin.get(f"/admin/cases/{ticket}").data
            staff_clip = f"/staff/cases/{ticket}/recordings/{rid}/audio"
            assert agent.get(staff_clip).status_code == 403
            assert agent.post(f"/staff/cases/{ticket}/claim", json={}).status_code == 200
            with agent.get(staff_clip) as response:
                assert response.status_code == 200
            assert admin.post(f"/admin/cases/{ticket}/history", json={"operation": "delete_ticket"}).status_code == 204
        else:
            assert admin.post(clip[:-6], json={"operation": "delete"}).status_code == 200
        assert admin.get(clip).status_code == 410
        assert clip.encode() not in admin.get("/admin?tab=voice-recordings").data

    rid = start(True)
    assert push(visitor, rid).status_code == 204
    assert admin.post(f"/admin/voice-recordings/{rid}", json={"label": "만료 테스트"}).status_code == 200
    with app.app_context():
        run("UPDATE recording SET expires_at=UTC_TIMESTAMP(6) WHERE recording_id=%s", (rid,))
        db().commit()
        purge()
        assert one("SELECT state FROM recording WHERE recording_id=%s", (rid,))["state"] == "READY"
        run("UPDATE recording SET expires_at=UTC_TIMESTAMP(6) WHERE recording_id=%s", (rid,))
        db().commit()
    assert admin.get(f"/admin/voice-recordings/{rid}/audio").status_code == 410
    with app.app_context():
        purge()
        assert one("SELECT state,object_key,label FROM recording WHERE recording_id=%s", (rid,)) == {
            "state": "DESTROYED", "object_key": None, "label": None}


if __name__ == "__main__":
    test_voice_handoff()
    test_voice_recordings()
    print("PASS voice setup errors, visitor isolation, concurrent handoff, phone queue and session end")
    print("PASS consent, isolated/retried PCM, WAV playback and audit, AI-only and handed-off recordings, recovery and expiry")
