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
    partner.call("/cases", {}, 400)
    customer.call("/cases", {}, 401)
    for client, name in [(admin, "admin"), (agent, "agent1"), (next_agent, "agent2")]:
        client.call("/staff/sessions", {"login_id": name, "password": env["DEMO_" + name.upper() + "_PASSWORD"]})
    staff = {row["login_id"]: row["staff_id"] for row in admin.call("/staff/agents")["items"]}
    assert "상담 현황".encode() in admin.call("/portal")
    origin = {"support_subject_ref": uid(), "intake_request_id": uid()}
    web = partner.call("/cases", origin, 201)["case_id"]
    assert partner.call("/cases", origin)["case_id"] == web
    assert partner.call("/cases?support_subject_ref=" + origin["support_subject_ref"])["items"][0]["case_id"] == web
    dashboard = "/portal/updates?tab=dashboard"
    pending_ticket = f'data-ticket-id="{web}"'
    assert pending_ticket in agent.call(dashboard)["html"]
    assert pending_ticket in next_agent.call(dashboard)["html"]
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
    admin.call("/staff/cases/" + web + "/assignee", {"new_agent_id": staff["agent1"]})
    assert "웹채팅 상담".encode() in agent.call("/portal/" + web)
    msg = {"client_message_id": uid(), "body": "검수용 문의"}
    first = customer.call(room + "/messages", msg, 201)
    assert customer.call(room + "/messages", msg)["message_id"] == first["message_id"]
    agent.call(room + "/messages", {"client_message_id": uid(), "body": "접수했습니다."}, 201)
    assert len(customer.call(room + "/messages")["items"]) == 2
    agent.call("/staff/cases/" + web + "/close", {"result_note": "안내 완료"})
    agent.call(claim, {}, 409)
    assert customer.call(room + "/messages")["read_only"]
    customer.call(room + "/messages", {"client_message_id": uid(), "body": "새 문의"}, 409)
    renewed = partner.call(room + "/entry-codes", subject, 201)["entry_code"]
    assert customer.call("/chat/sessions", {"entry_code": renewed})["read_only"]
    print("PASS exchange intake, one-time entry, chat retries and read-only closure", flush=True)

    phone = admin.call("/staff/phone-cases", {}, 201)["case_id"]
    path = "/staff/cases/" + phone
    assert agent.call(path + "/claim", {})["current_agent_id"] == staff["agent1"]
    assert "전화 응대 기록".encode() in agent.call("/portal/" + phone)
    form, mime, wav = audio_upload(uid())
    agent.call(path + "/recordings", form, 409, mime)
    notes = {"call_at": "2026-01-05T01:00:00Z", "consent_state": "AGREED", "inquiry": "문의",
             "guidance": "안내", "followup_required": False}
    agent.call(path + "/phone-response", notes)
    rec = agent.call(path + "/recordings", form, 201, mime)["recording_id"]
    assert agent.call(path + "/recordings", form, 200, mime)["recording_id"] == rec
    audio = path + "/recordings/" + rec + "/audio"
    assert agent.call(audio) == wav
    admin.call(audio, expect=403)
    admin.call(path + "/assignee", {"new_agent_id": staff["agent2"]})
    agent.call(audio, expect=403)
    assert next_agent.call(audio) == wav
    next_agent.call(path + "/close", {"result_note": "안내 완료"})
    next_agent.call(path + "/phone-response", notes, 409)
    manual = "/staff/cases/" + admin.call("/staff/phone-cases", {}, 201)["case_id"]
    admin.call(manual + "/assignee", {"new_agent_id": uid()}, 422)
    admin.call(manual + "/assignee", {"new_agent_id": staff["agent1"]})
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
            if detail["recordings"][0]["state"] == "DESTROYED" and detail["phone_response"] is None:
                break
            time.sleep(2)
        else:
            raise AssertionError("Automatic retention cleanup did not finish within the requested interval")
        next_agent.call(audio, expect=410)
        assert next_agent.call(path)["result_note"] is None and not customer.call(room + "/messages")["items"]
        print("PASS automatic recording and case-content purge")


if __name__ == "__main__":
    main()
