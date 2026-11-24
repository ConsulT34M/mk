"""Exercise the running HTTPS prototype with synthetic cases in a disposable stack."""
import argparse
import io
import json
import ssl
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, HTTPSHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, base, context, token=None):
        self.base = base
        self.browser = build_opener(ProxyHandler({}), HTTPSHandler(context=context), HTTPCookieProcessor(CookieJar()))
        self.headers = {"Authorization": "Bearer " + token} if token else {}

    def call(self, path, body=None, expect=200, content_type="application/json"):
        headers = dict(self.headers)
        if body is not None:
            headers["Content-Type"] = content_type
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
        request = Request(self.base + path, data=body, headers=headers)
        try:
            result = self.browser.open(request, timeout=10)
        except HTTPError as error:
            result = error
        with result:
            raw = result.read()
            data = json.loads(raw) if result.headers.get_content_type() == "application/json" else raw
            allowed = (expect,) if isinstance(expect, int) else expect
            assert result.status in allowed, (path, result.status, allowed, data.get("code") if isinstance(data, dict) else None)
            return data


def uid():
    return str(uuid4())


def audio_upload(request_id):
    wav = io.BytesIO()
    with wave.open(wav, "wb") as sound:
        sound.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        sound.writeframes(b"\0\0" * 160)
    marker = uid()
    form = (
        f'--{marker}\r\nContent-Disposition: form-data; name="upload_request_id"\r\n\r\n{request_id}\r\n'
        f'--{marker}\r\nContent-Disposition: form-data; name="file"; filename="sample.wav"\r\nContent-Type: audio/wav\r\n\r\n'
    ).encode() + wav.getvalue() + f"\r\n--{marker}--\r\n".encode()
    return form, "multipart/form-data; boundary=" + marker, wav.getvalue()


def check_admin_accounts(base, context, admin, agent, partner):
    worker = Client(base, context)
    login = "qa-" + uid()[:8]
    password = uid()
    credentials = {"login_id": login, "password": password}
    worker.call("/admin/agents", credentials, 401)
    agent.call("/admin", expect=403)
    agent.call("/admin/agents", credentials, 403)
    key = admin.call("/admin/agents", credentials, 201)["staff_id"]
    path = "/admin/agents/" + key
    admin.call("/admin/agents", credentials, 409)
    assert worker.call("/staff/sessions", credentials)["destination"] == "/portal"
    credentials = {"login_id": login + "-renamed", "password": uid()}
    admin.call(path, credentials)
    worker.call("/staff/cases", expect=401)
    worker.call("/staff/sessions", {"login_id": credentials["login_id"], "password": password}, 401)
    worker.call("/staff/sessions", credentials)
    admin.call(path + "/quota", {"daily": -1, "weekly": 3, "monthly": 10}, 400)
    admin.call(path + "/quota", {"daily": 2, "weekly": 3, "monthly": 10})
    dashboard = "/portal/updates?tab=dashboard"
    assert "0 / 2건" in worker.call(dashboard)["quota_html"]
    web = partner.call("/cases", {"support_subject_ref": uid(), "intake_request_id": uid()}, 201)["case_id"]
    worker.call("/staff/cases/" + web + "/claim", {})
    worker.call("/staff/cases/" + web + "/close", {"result_note": "완료"})
    assert "1 / 2건" in worker.call(dashboard)["quota_html"]
    phone = partner.call("/cases", {"channel": "PHONE", "support_subject_ref": uid(), "intake_request_id": uid()}, 201)["case_id"]
    worker.call("/staff/cases/" + phone + "/claim", {})
    admin.call(path + "/status", {"status": "inactive"})
    worker.call("/staff/cases", expect=401)
    worker.call("/staff/sessions", credentials, 401)
    assert phone in agent.call(dashboard)["html"]
    admin.call("/staff/cases/" + phone + "/assignee", {"new_agent_id": key}, 422)
    admin.call(path + "/status", {"status": "active"})
    worker.call("/staff/sessions", credentials)
    history = worker.call("/staff/cases/" + phone + "/claim", {})
    assert history["current_agent_id"] == key
    assert [a["login_id"] for a in worker.call("/staff/cases/" + phone)["assignments"]] == [credentials["login_id"], None, credentials["login_id"]]
    assert "배정 2건" in worker.call(dashboard)["quota_html"]
    assert worker.call("/staff/cases/" + web)["case"]["status"] == "CLOSED"
    log = admin.call("/admin?tab=audit").decode()
    assert all(title in log for title in ("비밀번호 재설정", "계정 비활성화", "상담 배정 해제", "할당량 변경"))
    assert password not in log and credentials["password"] not in log
    print("PASS admin account lifecycle, session revocation, requeue, quota totals and change log", flush=True)


def check_staff_notes(admin, agent, other, customer, web):
    note = {"case_id": web, "request_id": uid(), "body": "직원 전용 " + uid()}
    detail = "/staff/cases/" + web
    updates = "/portal/updates?tab=desk&case_id=" + web
    chat = "/cases/" + web + "/messages"
    original_chat = customer.call(chat)
    customer.call("/admin/notes", note, 401)
    agent.call("/admin/notes", note, 403)
    admin.call("/admin/notes", {**note, "body": " "}, 400)
    note_id = admin.call("/admin/notes", note, 201)["note_id"]
    assert admin.call("/admin/notes", note)["note_id"] == note_id
    assert note["body"].encode() in admin.call("/admin?tab=history")
    saved, = agent.call(detail)["staff_notes"]
    assert saved["note_id"] == note_id and saved["login_id"] == "admin"
    assert note["body"] in agent.call(updates)["notes_html"] and note["body"] not in other.call(updates)["notes_html"]
    edit = f"/admin/cases/{web}/notes/{note_id}"
    changes = {"body": "직원용 정정 " + uid(), "updated_at": saved["updated_at"]}
    agent.call(edit, changes, 403)
    admin.call(edit, changes)
    admin.call(edit, {**changes, "operation": "delete"}, 409)
    saved, = agent.call(detail)["staff_notes"]
    assert saved["body"] == changes["body"] and customer.call(chat) == original_chat
    delete = {"operation": "delete", "updated_at": saved["updated_at"]}
    admin.call(edit, delete)
    admin.call(edit, delete)
    admin.call("/admin/notes", note, 409)
    assert not agent.call(detail)["staff_notes"] and changes["body"].encode() not in admin.call("/admin?tab=history")
    assert customer.call(chat) == original_chat
    audit = admin.call("/admin?tab=audit").decode()
    assert all(action in audit for action in ("직원용 기록 추가", "직원용 기록 수정", "직원용 기록 삭제"))
    assert changes["body"] not in audit and note["body"] not in audit
    print("PASS staff note CRUD, retry, conflicting edit, agent visibility and customer chat isolation", flush=True)


def check_ticket_deletion(admin, agent, customer, partner):
    subject = {"support_subject_ref": uid()}
    intake = {**subject, "intake_request_id": uid()}
    ticket = partner.call("/cases", intake, 201)["case_id"]
    room = "/cases/" + ticket
    manage = "/admin" + room
    codes = [partner.call(room + "/entry-codes", subject, 201)["entry_code"] for _ in range(2)]
    customer.call("/chat/sessions", {"entry_code": codes[0]})
    customer.call(room + "/messages", {"client_message_id": uid(), "body": "삭제 검증"}, 201)
    note = {"case_id": ticket, "request_id": uid(), "body": "삭제 검증 " + uid()}
    admin.call("/admin/notes", note, 201)
    listing = "/admin?tab=history&channel=WEB"
    assert (manage + "?channel=WEB#history-editor").encode() in admin.call(listing)
    delete = {"operation": "delete_ticket"}
    agent.call(manage + "/history", delete, 403)
    customer.call(manage + "/history", delete, 401)
    assert ticket in agent.call("/portal/updates?tab=dashboard")["html"]
    for _ in range(2):
        admin.call(manage + "/history", delete, 204)
    assert ticket.encode() not in admin.call(listing) and ticket.encode() not in admin.call("/admin")
    assert note["body"].encode() not in admin.call(listing)
    assert ticket not in agent.call("/portal/updates?tab=dashboard")["html"]
    assert not partner.call("/cases?support_subject_ref=" + subject["support_subject_ref"])["items"]
    assert partner.call("/cases", intake, 410)["code"] == "TICKET_DELETED"
    partner.call(room + "/entry-codes", subject, 404)
    customer.call("/chat/sessions", {"entry_code": codes[1]}, 410)
    customer.call(room + "/messages", expect=401)
    agent.call("/staff" + room + "/claim", {}, 404)
    admin.call(manage, expect=404)
    admin.call("/admin/notes", note, 404)
    audit = admin.call("/admin?tab=audit").decode()
    assert audit.count(f"#{ticket[:8].upper()} · 삭제됨") == 2  # note creation and one ticket deletion
    assert manage not in audit and note["body"] not in audit
    print("PASS ticket deletion, list and queue removal, revoked chat access, intake retry and audit", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--wait-purge", type=int, default=0, help="Seconds to wait for the configured retention cycle.")
    args = parser.parse_args()
    base = "https://127.0.0.1:" + str(args.port)
    context = ssl.create_default_context(cafile=str(args.cert))
    env = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if "=" in line)
    partner = Client(base, context, env["PARTNER_TOKEN"])
    admin, agent, next_agent, customer = [Client(base, context) for _ in range(4)]
    assert customer.call("/health")["status"] == "ok"
    assert "직원 로그인".encode() in customer.call("/")
    assert "직원 로그인".encode() in customer.call("/admin")
    partner.call("/cases", {}, 400)
    customer.call("/cases", {}, 401)
    for client, name, password in [(admin, "admin", env["DEMO_ADMIN_PASSWORD"]), (agent, "test", "test"),
                                   (next_agent, "agent2", env["DEMO_AGENT2_PASSWORD"])]:
        result = client.call("/staff/sessions", {"login_id": name, "password": password})
        assert result["destination"] == ("/admin" if name == "admin" else "/portal")
    check_admin_accounts(base, context, admin, agent, partner)
    check_ticket_deletion(admin, agent, customer, partner)
    staff = {row["login_id"]: row["staff_id"] for row in admin.call("/staff/agents")["items"]}
    assert "관리자 대시보드".encode() in admin.call("/portal")
    admin.call("/staff/phone-cases", {}, 404)
    origin = {"support_subject_ref": uid(), "intake_request_id": uid()}
    web = partner.call("/cases", origin, 201)["case_id"]
    assert partner.call("/cases", origin)["case_id"] == web
    partner.call("/cases", {**origin, "channel": "PHONE"}, 409)
    partner.call("/cases", {**origin, "support_subject_ref": uid()}, 409)
    partner.call("/cases", {**origin, "channel": "EMAIL"}, 400)
    assert partner.call("/cases?support_subject_ref=" + origin["support_subject_ref"])["items"][0]["case_id"] == web
    dashboard = "/portal/updates?tab=dashboard"
    pending_ticket = f'data-ticket-id="{web}"'
    assert pending_ticket in agent.call(dashboard)["html"]
    assert pending_ticket in next_agent.call(dashboard)["html"]
    handoff = "/staff/cases/" + web + "/assignee"
    assert admin.call(handoff, {"new_agent_id": staff["test"]}, 409)["code"] == "TICKET_NOT_CLAIMED"
    assert handoff.encode() not in admin.call("/admin")
    customer.call(dashboard, expect=401)
    admin.call(dashboard, expect=403)
    claim = "/staff/cases/" + web + "/claim"
    admin.call(claim, {}, 403)
    customer.call(claim, {}, 401)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda client: client.call(claim, {}, (200, 409)), [agent, next_agent]))
    assert sum(result.get("case_id") == web for result in results) == 1
    assert sum(result.get("code") == "TICKET_TAKEN" for result in results) == 1
    winner, other = (agent, next_agent) if results[0].get("case_id") == web else (next_agent, agent)
    assert winner.call(claim, {})["case_id"] == web
    assert len(winner.call("/staff/cases/" + web)["assignments"]) == 1
    other.call("/staff/cases/" + web, expect=403)
    assert pending_ticket not in other.call(dashboard)["html"]
    assert f'data-case-id="{web}"' in winner.call("/portal/updates?tab=desk")["html"]
    print("PASS shared ticket queue, single concurrent owner, safe retry and desk visibility", flush=True)
    subject = {"support_subject_ref": origin["support_subject_ref"]}
    room = "/cases/" + web
    code = partner.call(room + "/entry-codes", subject, 201)["entry_code"]

    def try_entry(_):
        attempt = Client(base, context)
        result = attempt.call("/chat/sessions", {"entry_code": code}, (200, 410))
        return result.get("case_id") == web

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(try_entry, range(2))) == 1
    code = partner.call(room + "/entry-codes", subject, 201)["entry_code"]
    customer.call("/chat/sessions", {"entry_code": code})
    assert "고객 상담방".encode() in customer.call("/chat")
    admin.call(handoff, {"new_agent_id": staff["test"]})
    assert "웹채팅 상담".encode() in agent.call("/portal/" + web)
    msg = {"client_message_id": uid(), "body": "검수용 문의"}
    first = customer.call(room + "/messages", msg, 201)
    assert customer.call(room + "/messages", msg)["message_id"] == first["message_id"]
    agent.call(room + "/messages", {"client_message_id": uid(), "body": "접수했습니다."}, 201)
    assert len(customer.call(room + "/messages")["items"]) == 2
    check_staff_notes(admin, agent, next_agent, customer, web)
    history = "/admin/cases/" + web
    manage = history + "/messages/" + first["message_id"]
    assert msg["body"].encode() in admin.call(history)
    agent.call(manage, {"body": "정정"}, 403)
    admin.call(manage, {"body": "정정"})
    assert customer.call(room + "/messages")["items"][0]["body"] == "정정"
    admin.call(manage, {"operation": "delete"})
    assert len(customer.call(room + "/messages")["items"]) == 1
    customer.call(room + "/messages", msg, 410)
    agent.call("/staff/cases/" + web + "/close", {"result_note": "안내 완료"})
    admin.call(history + "/history", {"result_note": "처리 결과 정정"})
    assert agent.call("/staff/cases/" + web)["result_note"] == "처리 결과 정정"
    admin.call("/admin/notes", {"case_id": web, "request_id": uid(), "body": "종결 후 직원용 기록"}, 201)
    admin.call(history + "/history", {"operation": "delete"})
    assert not agent.call("/staff/cases/" + web)["staff_notes"]
    assert not customer.call(room + "/messages")["items"]
    agent.call(claim, {}, 409)
    assert customer.call(room + "/messages")["read_only"]
    customer.call(room + "/messages", {"client_message_id": uid(), "body": "새 문의"}, 409)
    renewed = partner.call(room + "/entry-codes", subject, 201)["entry_code"]
    assert customer.call("/chat/sessions", {"entry_code": renewed})["read_only"]
    print("PASS exchange intake, one-time entry, chat retries and read-only closure", flush=True)

    phone_request = {**origin, "channel": "PHONE", "intake_request_id": uid()}
    admin.call("/cases", phone_request, 401)
    with ThreadPoolExecutor(2) as pool:
        first_id, retry_id = pool.map(lambda _: partner.call("/cases", phone_request, (200, 201))["case_id"], range(2))
    phone = first_id
    assert retry_id == phone
    subject_cases = "/cases?support_subject_ref=" + origin["support_subject_ref"]
    assert partner.call(subject_cases + "&channel=PHONE")["items"] == [{"case_id": phone, "status": "QUEUED"}]
    assert partner.call(subject_cases)["items"] == [{"case_id": web, "status": "CLOSED"}]
    partner.call("/cases/" + phone + "/entry-codes", subject, 409)
    path = "/staff/cases/" + phone
    assert phone in agent.call(dashboard)["html"]
    assert agent.call(path + "/claim", {})["current_agent_id"] == staff["test"]
    assert (path + "/assignee").encode() in admin.call("/admin")
    admin.call(path + "/assignee", {"new_agent_id": staff["agent2"], "previous_agent_id": ""}, 409)
    assert "전화 응대 기록".encode() in agent.call("/portal/" + phone)
    form, mime, wav = audio_upload(uid())
    agent.call(path + "/recordings", form, 409, mime)
    notes = {"call_at": "2026-01-05T01:00:00Z", "consent_state": "AGREED", "inquiry": "문의",
             "guidance": "안내", "followup_required": False}
    agent.call(path + "/phone-response", notes)
    assert partner.call("/cases", phone_request)["case_id"] == phone
    assert agent.call(path)["phone_response"]["consent_state"] == "AGREED"
    rec = agent.call(path + "/recordings", form, 201, mime)["recording_id"]
    assert agent.call(path + "/recordings", form, 200, mime)["recording_id"] == rec
    audio = path + "/recordings/" + rec + "/audio"
    assert agent.call(audio) == wav
    admin.call(audio, expect=403)
    history = "/admin/cases/" + phone
    admin.call(history + "/history", {"guidance": "관리자 정정"})
    assert agent.call(path)["phone_response"]["guidance"] == "관리자 정정"
    admin.call(history + "/history", {"guidance": None})
    assert agent.call(path)["phone_response"]["guidance"] is None
    admin.call(history + "/history", {"guidance": "안내"})
    second_form, second_mime, _ = audio_upload(uid())
    second = agent.call(path + "/recordings", second_form, 201, second_mime)["recording_id"]
    manage = history + "/recordings/" + second
    agent.call(manage, {"label": "검수 녹취"}, 403)
    admin.call(manage, {"label": "검수 녹취"})
    assert "검수 녹취".encode() in admin.call(history)
    assert phone.encode() in admin.call("/admin?tab=history&channel=RECORDINGS")
    assert any(r["label"] == "검수 녹취" for r in agent.call(path)["recordings"])
    admin.call(manage, {"operation": "delete"})
    admin.call(manage, {"operation": "delete"})
    agent.call(path + "/recordings/" + second + "/audio", expect=410)
    admin.call(manage, {"label": "다시 저장"}, 410)
    change = {"previous_agent_id": staff["test"], "new_agent_id": staff["agent2"]}
    handoff_note = {"case_id": phone, "request_id": uid(), "body": "전화 인계 " + uid()}
    admin.call("/admin/notes", handoff_note, 201)
    admin.call(path + "/assignee", change)
    admin.call(path + "/assignee", change)
    assert "test → agent2".encode() in admin.call("/admin?tab=audit")
    assert len(next_agent.call(path)["assignments"]) == 2
    assert next_agent.call(path)["staff_notes"][0]["body"] == handoff_note["body"]
    assert handoff_note["body"] not in agent.call("/portal/updates?tab=desk&case_id=" + phone)["notes_html"]
    agent.call(audio, expect=403)
    assert next_agent.call(audio) == wav
    next_agent.call(path + "/close", {"result_note": "안내 완료"})
    admin.call(path + "/assignee", {"new_agent_id": staff["test"]}, 409)
    assert (path + "/assignee").encode() not in admin.call("/admin")
    next_agent.call(path + "/phone-response", notes, 409)
    intake = partner.call("/cases", {"channel": "PHONE", "support_subject_ref": uid(), "intake_request_id": uid()}, 201)
    manual = "/staff/cases/" + intake["case_id"]
    agent.call(manual + "/claim", {})
    admin.call(manual + "/assignee", {"new_agent_id": uid()}, 422)
    agent.call(manual + "/close", {"result_note": "수기 처리"}, 422)
    agent.call(manual + "/phone-response", {**notes, "consent_state": "REFUSED"})
    agent.call(manual + "/recordings", form, 409, mime)
    agent.call(manual + "/close", {"result_note": "수기 처리"})
    next_agent.call("/staff/sessions/logout", {}, 204)
    next_agent.call(path, expect=401)
    print("PASS consent, WAV retry, playback, reassignment, closure and logout", flush=True)

    if args.wait_purge:
        next_agent.call("/staff/sessions", {"login_id": "agent2", "password": env["DEMO_AGENT2_PASSWORD"]})
        limit = time.monotonic() + args.wait_purge
        while time.monotonic() < limit:
            detail = next_agent.call(path)
            if all(r["state"] == "DESTROYED" for r in detail["recordings"]) and detail["phone_response"] is None:
                break
            time.sleep(2)
        else:
            raise AssertionError("Automatic retention cleanup did not finish within the requested interval")
        next_agent.call(audio, expect=410)
        assert next_agent.call(path)["result_note"] is None and not customer.call(room + "/messages")["items"]
        admin.call(history + "/history", {"guidance": "만료 후 수정"}, 410)
        admin.call("/admin/notes", handoff_note, 410)
        assert not next_agent.call(path)["staff_notes"] and handoff_note["body"].encode() not in admin.call("/admin?tab=history")
        admin.call(history + "/history", {"operation": "delete_ticket"}, 204)
        next_agent.call(path, expect=404)
        assert phone not in next_agent.call("/portal/updates?tab=closed")["html"]
        print("PASS automatic recording and case-content purge")


if __name__ == "__main__":
    main()
