"""격리된 DB에서 고객 확인·티켓 접수와 관리자 파이차트 집계를 검사한다."""
import os
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from app import app
from app.db import db, one, run
from app.desk_reports import midnight, today
from app.queries import ai_outcomes


def uid():
    return str(uuid4())


def check():
    client, admin = app.test_client(), app.test_client()
    headers = {"Authorization": "Bearer " + os.environ["PARTNER_TOKEN"], "X-Support-Subject": uid()}
    other = dict(headers, **{"X-Support-Subject": uid()})
    admin.post("/staff/sessions", json={"login_id": "admin", "password": os.environ["DEMO_ADMIN_PASSWORD"]})
    chat_path, voice_path = "/partner/ai-chat", "/inquiry/voice/sessions"

    def post(path, body=None, owner=headers):
        return client.post(path, json=body or {}, headers=owner)

    def turn(chat):
        return post(chat_path + "/messages", {"chat_id": chat, "request_id": uid(), "body": "상담 문의"})

    solved, handed, unknown = uid(), uid(), uid()
    with patch("app.ai_chat.generate_reply", return_value="문의 답변"):
        for chat in (solved, handed, unknown, unknown):
            assert turn(chat).status_code == 201
    resolve, handoff = f"{chat_path}/{solved}/resolve", f"{chat_path}/{handed}/handoff"
    assert post(resolve, owner=other).status_code == 403
    assert post(resolve).status_code == post(resolve).status_code == 200
    assert turn(solved).status_code == 409
    assert post(f"{chat_path}/{solved}/handoff", {"intake_request_id": uid()}).status_code == 409
    intake = {"support_subject_ref": headers["X-Support-Subject"], "intake_request_id": uid()}
    ticket = post("/cases", intake).json["case_id"]
    assert post(handoff, intake, other).status_code == 403
    assert post(handoff, intake).json["case_id"] == ticket
    assert post(handoff, {"intake_request_id": uid()}).json["case_id"] == ticket
    assert post(f"{chat_path}/{handed}/resolve").status_code == 409
    with app.app_context():
        run("UPDATE support_case SET closed_at=UTC_TIMESTAMP(6) WHERE case_id=%s", (ticket,))
        db().commit()
    assert post(f"{chat_path}/{unknown}/handoff", intake).json["code"] == "AI_HANDOFF_ROOM_CLOSED"
    with app.app_context():
        assert not ai_outcomes.get("WEB", unknown)

    with patch("app.voice.client_secret", return_value="fixture"):
        calls = [post(voice_path).json["voice_id"] for _ in range(3)]
    paths = [f"{voice_path}/{call}" for call in calls]
    assert post(paths[0]+"/resolve", owner=other).status_code == 404
    assert post(paths[0]+"/resolve").status_code == post(paths[0]+"/resolve").status_code == 200
    assert post(paths[0]+"/handoff").status_code == 409
    assert post(paths[1]+"/handoff").status_code == 201 and post(paths[1]+"/handoff").status_code == 200
    assert post(paths[1]+"/resolve").status_code == 409
    for path in paths:
        assert post(path+"/end").status_code == 204

    day = today() - timedelta(days=21)
    start, end = midnight(day), midnight(day+timedelta(days=1))
    with app.app_context():
        # 한 채팅의 두 번째 답변은 다음 날이며, 과거 음성 인계는 티켓만 남은 상황이다.
        run("UPDATE ai_chat_turn SET created_at=%s WHERE chat_id IN (%s,%s,%s)", (start, solved, handed, unknown))
        run("UPDATE ai_chat_turn SET created_at=%s WHERE chat_id=%s ORDER BY turn_id DESC LIMIT 1", (end, unknown))
        run("UPDATE audit_event SET created_at=%s WHERE recording_id IN (%s,%s,%s)", (start, *calls))
        run("DELETE FROM ai_consultation_outcome WHERE channel='PHONE' AND session_id=%s", (calls[1],))
        db().commit()
        for channel in ("PHONE", "WEB"):
            assert {r["status"]:r["n"] for r in ai_outcomes.counts(start, end, channel)} == {"RESOLVED":1,"HANDOFF":1,"UNKNOWN":1}
        assert not ai_outcomes.counts(end, end+timedelta(days=1), "WEB")
        assert not one("SELECT 1 FROM ai_consultation_outcome WHERE session_id=%s", (calls[2],))
    def panel(**args):
        response = admin.get("/admin", query_string={"day":day.isoformat(), **args})
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert html.index("시간대별 접수") < html.index('id="ai-outcomes"') < html.index("문의 유형 분포")
        return html.split('id="ai-outcomes"', 1)[1].split("</section>", 1)[0]
    assert "50%" in panel() and "결과 미확인 2건" in panel()
    assert "결과 미확인 1건" in panel(channel="PHONE")
    transfers = panel(ai_view="handoff")
    assert "채팅 전환" in transfers and "통화 전환" in transfers and ">2건<" in transfers
    assert ">—<" in panel(day=(day-timedelta(days=1)).isoformat())


if __name__ == "__main__":
    check()
    print("PASS AI resolution, actual chat and voice handoffs, retries, ownership, historical calls, KST cohorts and chart tabs")
