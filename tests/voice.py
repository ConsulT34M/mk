"""Run inside the disposable app container with MySQL. OpenAI is mocked throughout."""
import base64
import io
import json
import os
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from importlib import import_module
from unittest.mock import patch
from urllib.error import HTTPError

from app import app, recording_store, voice_summaries
from app.common import INQUIRY_COOKIE
from app.db import db, one, run
from app.queries import voice as voice_queries
from app.recordings import purge
from app.voice_recording import partial_path


def test_call_opening():
    admin, visitor = app.test_client(), app.test_client()
    admin.post("/staff/sessions", json={"login_id": "admin", "password": os.environ["DEMO_ADMIN_PASSWORD"]})
    visitor.get("/inquiry")
    with app.app_context():
        saved = voice_queries.policy()
    settings = "/admin/ai-settings"
    ringback = "/inquiry/voice/ringback"
    previous = dict(enabled=bool(saved["enabled"]), offer_handoff=bool(saved["offer_handoff"]),
                    instructions=saved["instructions"], greeting=saved["greeting"], ringback_enabled=bool(saved["ringback_enabled"]))
    policy = {**previous, "enabled": True, "ringback_enabled": False, "greeting": "안녕하십니까. 예약 상담을 도와드릴게요."}
    try:
        assert admin.post(settings, json=policy).status_code == 200
        assert policy["greeting"] in admin.get("/admin?tab=ai").get_data(as_text=True)
        assert visitor.get(ringback).status_code == 204
        for invalid in ({"greeting": "  "}, {"greeting": "가" * 201}, {"ringback_enabled": "false"}):
            assert admin.post(settings, json={**policy, **invalid}).status_code == 400
        # 구형 관리 화면의 저장 요청은 첫 인사말과 대기음 설정을 덮어쓰지 않는다.
        assert admin.post(settings, json={k: policy[k] for k in ("enabled", "instructions", "offer_handoff")}).status_code == 200
        with patch("app.voice.urlopen", return_value=io.BytesIO(b'{"value":"test"}')), patch.dict(os.environ, OPENAI_API_KEY="fixture"):
            session = visitor.post("/inquiry/voice/sessions", json={})
        assert session.status_code == 201 and session.json["greeting"] == policy["greeting"]
        assert visitor.post(f"/inquiry/voice/sessions/{session.json['voice_id']}/end", json={}).status_code == 204
        assert visitor.get(ringback).status_code == 204
        assert admin.post(settings, json={**policy, "ringback_enabled": True}).status_code == 200
        with visitor.get(ringback) as sound:
            assert sound.status_code == 200 and sound.mimetype == "audio/mpeg" and sound.data
    finally:
        assert admin.post(settings, json=previous).status_code == 200


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
    assert visitor.post(handoff, json={"call_at": "2026-01-02T03:04"}).status_code == 400

    def request_handoff(_):
        tab = app.test_client()
        tab.set_cookie(INQUIRY_COOKIE, visitor.get_cookie(INQUIRY_COOKIE).value)
        return tab.post(handoff, json={"call_at": "2026-01-02T12:04:37.123+09:00"})

    with ThreadPoolExecutor(2) as pool:
        attempts = list(pool.map(request_handoff, range(2)))
    assert sorted(result.status_code for result in attempts) == [200, 201]
    ticket = attempts[0].json["case_id"]
    assert attempts[1].json["case_id"] == ticket
    assert visitor.post(handoff, json={"call_at": "2026-01-02T03:05:00Z"}).status_code == 200
    with app.app_context():
        case = one("SELECT channel,current_agent_id FROM visible_case WHERE case_id=%s", (ticket,))
        assert case == {"channel": "PHONE", "current_agent_id": None}
        assert voice_queries.phone_response(ticket)["call_at"] == datetime(2026, 1, 2, 3, 4, 37, 123000)
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
    pcm = bytes(range(32)) * 1024

    def start(consent, summarize=False):
        with patch.object(import_module("app.voice"), "urlopen", return_value=io.BytesIO(b'{"value":"test"}')), \
                patch.dict(os.environ, OPENAI_API_KEY="fixture-only"):
            result = visitor.post(base, json={"recording_consent": consent, "summary_consent": summarize})
        assert result.status_code == 201
        return result.json["voice_id"]

    def push(client, rid, offset=0, data=pcm, headers=None):
        return client.post(f"{base}/{rid}/recording/{offset}", data=data, content_type="application/octet-stream", headers=headers)

    note = "출금 지연 사유 확인 요청"
    legacy = "**[고객 문의]**: (출금 지연 사유 확인 요청함.)\nAI 안내 · 테스트 안내\n후속 조치 · 확인되지 않음"
    assert voice_summaries.request_line(legacy) == note
    assert voice_summaries.request_line("고객 문의: 확인되지 않음\nAI 안내: 상담 종료") == voice_summaries.NO_REQUEST
    def complete(request, **options):
        assert request.full_url == "https://api.openai.com/v1/chat/completions"
        params = json.loads(request.data)
        assert params["modalities"] == ["text"] and params["store"] is False
        answer = {"choices": [{"finish_reason": "stop", "message": {"content": f"({note})"}}]}
        return io.BytesIO(json.dumps(answer).encode())

    assert visitor.post(base, json={"recording_consent": False, "summary_consent": True}).status_code == 400
    refused = start(False)
    assert push(visitor, refused).json["code"] == "RECORDING_CONSENT_REQUIRED"
    assert visitor.post(f"{base}/{refused}/end", json={}).status_code == 204
    assert admin.post(f"/admin/voice-recordings/{refused}/summary", json={}).status_code == 404
    for handoff in (False, True):
        rid = start(True, summarize=handoff)
        session = f"{base}/{rid}"
        clip = f"/admin/voice-recordings/{rid}/audio"
        assert push(other, rid).status_code == 404
        assert push(visitor, rid, 2).status_code == 409
        started_at = "2026-01-02T03:04:37.123Z"
        assert push(visitor, rid, headers={"X-Call-Started-At": started_at}).status_code == 204
        assert push(visitor, rid, headers={"X-Call-Started-At": "2026-01-02T03:05:00Z"}).status_code == 204
        assert push(visitor, rid, data=b"xxxx").status_code == 409
        assert push(visitor, rid, len(pcm)).status_code == 204
        assert admin.get(clip).status_code == 409 and agent.get(clip).status_code == 403
        if handoff:
            ticket = visitor.post(session + "/handoff", json={}).json["case_id"]
            with app.app_context():
                assert voice_queries.phone_response(ticket)["call_at"] == datetime(2026, 1, 2, 3, 4, 37, 123000)
        with patch.object(recording_store, "put_audio", side_effect=OSError("fixture")):
            assert visitor.post(session + "/end", json={}).status_code == 503
        assert visitor.post(session + "/end", json={}).json == {"recorded": True}
        assert visitor.post(session + "/end", json={}).status_code == 204
        summary_url = clip[:-6] + "/summary"
        assert other.post(summary_url, json={}).status_code == 401
        assert agent.post(summary_url, json={}).status_code == 403
        with app.app_context():
            queued = one("SELECT state FROM ai_call_summary WHERE recording_id=%s", (rid,))
            assert queued == ({"state": "QUEUED"} if handoff else None)
        if not handoff:
            assert admin.post(summary_url, json={}).status_code == 202
            assert admin.post(summary_url, json={}).json["state"] == "QUEUED"
            with app.app_context(), patch.dict(os.environ, OPENAI_API_KEY=""):
                assert voice_summaries.process_next_summary()
            assert "다시 시도" in admin.get("/admin?tab=ai-calls").get_data(as_text=True)
            assert admin.post(summary_url, json={}).status_code == 202
        with app.app_context(), patch.dict(os.environ, OPENAI_API_KEY="fixture-only"), \
                patch.object(voice_summaries, "CHUNK_SECONDS", 1), patch.object(voice_summaries, "urlopen", side_effect=complete) as upstream:
            assert voice_summaries.process_next_summary()
            assert not voice_summaries.process_next_summary()
            parts = []
            for i, call in enumerate(upstream.call_args_list):
                messages = json.loads(call.args[0].data)["messages"]
                if i:
                    assert messages[-2] == {"role": "assistant", "content": note}
                encoded = messages[-1]["content"][-1]["input_audio"]["data"]
                with wave.open(io.BytesIO(base64.b64decode(encoded))) as wav:
                    parts.append(wav.readframes(wav.getnframes()))
            assert len(parts) > 1 and b"".join(parts) == pcm + pcm
        assert admin.post(summary_url, json={}).status_code == 200
        history = admin.get("/admin?tab=ai-calls").get_data(as_text=True)
        assert note in history and 'datetime="2026-01-02T03:04:37.123000Z"' in history
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
            detail = f"/staff/cases/{ticket}"
            poll = f"/portal/updates?tab=calls&case_id={ticket}&summaries=1"
            history_page = "/portal?tab=recordings&start=2026-01-02&end=2026-01-02"
            assert agent.get(staff_clip).status_code == 403
            assert agent.get(detail).status_code == 403 and agent.get(poll).json["summaries"] == {}
            assert agent.post(f"/staff/cases/{ticket}/claim", json={}).status_code == 200
            with agent.get(staff_clip) as response:
                assert response.status_code == 200
            # 기존 형식의 요약과 보관 기한이 남은 녹취도 새 화면에서 읽을 수 있다.
            with app.app_context():
                run("UPDATE ai_call_summary SET summary=%s WHERE recording_id=%s", (legacy, rid))
                run("UPDATE recording SET expires_at=UTC_TIMESTAMP(6)-INTERVAL 30 DAY WHERE recording_id=%s", (rid,))
                db().commit()
                purge()
            assert agent.get(detail).json["recordings"][0]["summary"] == note
            assert agent.get(poll).json["summaries"] == {rid: note}
            assert note in agent.get(f"/portal/{ticket}").get_data(as_text=True)
            assert note in agent.get(history_page).get_data(as_text=True)
            assert note in admin.get("/admin?tab=ai-calls").get_data(as_text=True)
            peer = app.test_client()
            peer_id = peer.post("/staff/sessions", json={"login_id": "agent2", "password": os.environ["DEMO_AGENT2_PASSWORD"]}).json["staff_id"]
            assert peer.get(detail).status_code == 403 and peer.get(poll).json["summaries"] == {}
            assert note not in peer.get(history_page).get_data(as_text=True)
            assert admin.post(detail + "/assignee", json={"new_agent_id": peer_id}).status_code == 200
            assert agent.get(detail).status_code == 403 and agent.get(poll).json["summaries"] == {}
            assert peer.get(poll).json["summaries"] == {rid: note}
            assert peer.post(detail + "/phone-response", json={"consent_state": "REFUSED"}).status_code == 200
            assert peer.get(staff_clip).status_code == 410 and peer.get(poll).json["summaries"] == {}
            assert peer.get(detail).json["recordings"][0]["summary"] is None
            assert admin.post(f"/admin/cases/{ticket}/history", json={"operation": "delete_ticket"}).status_code == 204
        else:
            assert admin.post(clip[:-6], json={"operation": "delete"}).status_code == 200
        assert admin.get(clip).status_code == 410
        assert clip.encode() not in admin.get("/admin?tab=voice-recordings").data
        assert admin.post(summary_url, json={}).status_code in (404, 410)
        with app.app_context():
            purge()
            assert one("SELECT 1 FROM ai_call_summary WHERE recording_id=%s", (rid,)) is None

    rid = start(True, summarize=True)
    assert push(visitor, rid).status_code == 204
    assert admin.post(f"/admin/voice-recordings/{rid}", json={"label": "유휴 통화 복구"}).status_code == 200
    with app.app_context():
        run("UPDATE recording SET expires_at=UTC_TIMESTAMP(6)-INTERVAL 30 DAY WHERE recording_id=%s", (rid,))
        db().commit()
        with patch.object(recording_store, "put_audio", side_effect=OSError("temporary failure")):
            purge()
        assert partial_path(rid).exists()
        assert one("SELECT state FROM recording WHERE recording_id=%s", (rid,))["state"] == "PENDING"
        purge()
        assert one("SELECT state,expires_at FROM recording WHERE recording_id=%s", (rid,)) == {"state": "READY", "expires_at": None}
        def delete_during_summary(*args, **kwargs):
            with app.app_context():
                assert not voice_summaries.process_next_summary()
                assert admin.post(f"/admin/voice-recordings/{rid}", json={"operation": "delete"}).status_code == 200
            return complete(*args, **kwargs)
        with patch.dict(os.environ, OPENAI_API_KEY="fixture-only"), patch.object(voice_summaries, "urlopen", side_effect=delete_during_summary):
            assert voice_summaries.process_next_summary()
        assert one("SELECT summary FROM ai_call_summary WHERE recording_id=%s", (rid,))["summary"] is None
    assert admin.get(f"/admin/voice-recordings/{rid}/audio").status_code == 410
    with app.app_context():
        purge()
        assert one("SELECT state,object_key,label FROM recording WHERE recording_id=%s", (rid,)) == {
            "state": "DESTROYED", "object_key": None, "label": None}


if __name__ == "__main__":
    test_call_opening()
    test_voice_handoff()
    test_voice_recordings()
    print("PASS voice setup errors, visitor isolation, concurrent handoff, phone queue and session end")
    print("PASS editable first greeting and ringback settings for new calls")
    print("PASS consent, WAV playback, idle recovery, continued retention and explicit deletion")
    print("PASS one-line AI summaries, legacy display, staff reassignment, consent withdrawal and concurrent deletion")
