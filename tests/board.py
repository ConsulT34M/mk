"""Run inside a disposable MySQL-backed MK app."""
import os
from uuid import uuid4

from app import app


def uid():
    return str(uuid4())


def login(client, name, password):
    assert client.post("/staff/sessions", json={"login_id": name, "password": password}).status_code == 200


admin, agent, guest = (app.test_client() for _ in range(3))
login(admin, "admin", os.environ["DEMO_ADMIN_PASSWORD"])
login(agent, "test", "test")
notice = "교대 공지 " + uid()
assert admin.post("/admin/notices", json={"notice_id": uid(), "title": notice, "body": "공지 본문"}).status_code == 201
assert notice in agent.get("/portal?tab=dashboard").get_data(as_text=True)
assert notice in agent.get("/portal?tab=notices").get_data(as_text=True)
assert notice in agent.get("/portal/updates?tab=dashboard").json["notice_html"]
for tab in ("calls", "chats", "desk", "closed&channel=PHONE", "closed&channel=WEB", "stats", "recordings", "board"):
    page = agent.get("/portal?tab=" + tab)
    assert page.status_code == 200
    assert notice not in page.get_data(as_text=True) and b'id="live-notices"' not in page.data
for tab in ("calls", "chats", "desk", "closed"):
    assert "notice_html" not in agent.get("/portal/updates?tab=" + tab).json
ticket = admin.post("/cases", headers={"Authorization": "Bearer " + os.environ["PARTNER_TOKEN"]},
                    json={"channel": "WEB", "support_subject_ref": uid(), "intake_request_id": uid()}).json["case_id"]
assert agent.post(f"/staff/cases/{ticket}/claim", json={}).status_code == 200
for closed in (False, True):
    if closed:
        assert agent.post(f"/staff/cases/{ticket}/close", json={"result_note": "검증 완료"}).status_code == 200
    detail = agent.get("/portal/" + ticket)
    assert detail.status_code == 200 and b'id="live-notices"' not in detail.data
print("PASS notices only on the staff dashboard and notices tab, including selected and closed cases")

root = "/staff/board/posts"
assert guest.get("/portal?tab=board").status_code == 302
assert guest.post(root, json={}).status_code == 401
for author, author_tab, reader, reader_tab in (
    (agent, "/portal?tab=board", admin, "/admin?tab=board"),
    (admin, "/admin?tab=board", agent, "/portal?tab=board"),
):
    rid = uid()
    values = {"post_id": rid, "title": "업무 공유 " + rid, "body": "<b>테스트</b>\n다음 줄"}
    assert author.post(root, json={**values, "title": " "}).status_code == 400
    created = author.post(root, json=values)
    assert created.status_code == 201 and created.json["post_id"] == rid
    assert author.post(root, json=values).status_code == 200
    assert reader.post(root, json=values).status_code == 409
    assert values["title"] in reader.get(reader_tab).get_data(as_text=True)
    author_page = author.get(created.json["location"]).get_data(as_text=True)
    reader_page = reader.get(reader_tab + "&post=" + rid).get_data(as_text=True)
    assert 'value="delete"' in author_page and 'value="delete"' not in reader_page
    assert "&lt;b&gt;테스트&lt;/b&gt;" in reader_page
    assert reader.get(reader_tab + "&post=" + rid + "&write=1").status_code == 403
    path = root + "/" + rid
    edit = {"operation": "edit", "revision": 1, "title": values["title"], "body": "수정한 내용"}
    delete = {"operation": "delete", "revision": 2}
    assert reader.post(path, json=edit).status_code == 403
    assert reader.post(path, json=delete).status_code == 403
    assert author.post(path, json=edit).status_code == 200
    assert author.post(path, json=edit).status_code == 200
    assert author.post(path, json={**edit, "body": "이전 화면"}).status_code == 409
    assert edit["body"] in reader.get(reader_tab + "&post=" + rid).get_data(as_text=True)
    assert author.post(path, json=delete).status_code == 200
    assert author.post(path, json=delete).status_code == 200
    assert author.post(root, json=values).status_code == 409
    assert reader.get(reader_tab + "&post=" + rid).status_code == 404
    assert values["title"] not in reader.get(reader_tab).get_data(as_text=True)
print("PASS shared staff board, escaped content, author-only edits and deletion, safe retries and stale edits")
