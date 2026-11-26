from collections import Counter

from flask import Blueprint, g, redirect, render_template, request, url_for
from pymysql.err import IntegrityError
from werkzeug.security import generate_password_hash

from mysql.migrations import init_admin, migrate_desk, migrate_recording_labels

from . import openai_settings
from .board import board_page
from .ai_usage import usage_report
from .cases import PHONE_RECORD_FIELDS, assignment_log, message_info, phone_info, save_phone_response, set_assignee, staff_notes
from .common import ApiError, admin, case_info, case_row, channel, content_visible, payload, staff_user, text_field, uuid_field
from .db import admin_change, new_id, stamp
from .desk_reports import OUTCOMES, TOPICS, channel_metrics, dashboard_charts, midnight, quota_starts, report_range, statistics, today
from .queries import ai_chat
from .queries import cases as case_queries
from .queries import chat as chat_queries
from .queries import recordings as recording_queries
from .queries import reports as report_queries
from .queries import staff as staff_queries
from .queries import voice as voice_queries
from .queries.reports import PERIODS
from .recordings import check_not_deleted, purge_case_content, stream_recording
from .voice_summaries import call_list

# 이 Blueprint의 화면과 변경 API는 모두 /admin 아래에 등록되고 require_admin 검사를 거친다.
management = Blueprint("management", __name__, url_prefix="/admin")


@management.before_request
def require_admin():
    # 모든 관리자 뷰 실행 전에 인증한 계정을 g에 담는다. 페이지 GET은 미로그인 시 로그인으로
    # 이동하고 권한이 부족하면 안내 화면을 표시한다. 변경 요청의 오류는 앱 공통 처리기로 보낸다.
    try:
        g.admin_user = admin()
    except ApiError as error:
        if request.method != "GET":
            raise
        if error.status == 401:
            return redirect(url_for("home"))
        return render_template("denied.html", user=staff_user(), error=error.message), error.status


def quota_rows(staff_id=None):
    return report_queries.quota_progress(quota_starts(), staff_id)


def agent_account(staff_id, lock=False):
    # 관리자 계정은 상담원 편집 대상에서 제외한다. 수정 요청은 계정 행을 잠근 상태로 검사를 이어간다.
    person = staff_queries.agent_account(staff_id, lock=lock)
    if not person:
        raise ApiError("AGENT_NOT_FOUND")
    return person


@management.get("", strict_slashes=False)
def index():
    # /admin의 tab 쿼리가 같은 템플릿에 표시할 화면을 정한다. 선택한 탭에서 쓸 자료만 data에 담는다.
    tab = request.args.get("tab", "overview")
    data = {}
    if tab in ("agents", "accounts"):
        # 계정 목록은 활성 여부로 거른다. 편집 대상은 필터와 별개로 전체 상담원에서 찾는다.
        tab = "agents"
        people = quota_rows()
        group = request.args.get("filter", "active")
        data.update(people=[p for p in people if group == "all" or bool(p["is_active"]) == (group != "inactive")], group=group)
        selected = request.args.get("edit")
        data["editor"] = {} if selected == "new" else next((p for p in people if p["staff_id"] == selected), None)
        if selected and selected != "new" and not data["editor"]:
            raise ApiError("AGENT_NOT_FOUND")
    elif tab == "board":
        return board_page(g.admin_user)
    elif tab == "notices":
        return redirect(url_for("management.index", tab="board", _anchor="board-notices"))
    elif tab == "org":
        # 수정 폼에 현재 직원별 팀 배정표(snapshot)를 전달한다. 저장할 때 이 값과 DB를 비교한다.
        teams = staff_queries.teams()
        members = staff_queries.team_roster()
        selected = request.args.get("team", teams[0]["team_id"] if teams else None)
        team = next((t for t in teams if t["team_id"] == selected), None)
        if selected and not team:
            raise ApiError("INVALID_INPUT")
        dates = report_range()
        data.update(teams=teams, members=members, team=team, dates=dates, snapshot={p["staff_id"]:p["team_id"] for p in members},
                    team_quota=report_queries.team_quota(selected) if team else None,
                    report=statistics(dates, team_id=selected) if team else None)
    elif tab == "ai":
        # 한국 시간 오늘의 음성 동의·거부 이벤트에서 통화 수를, 같은 접수 키의 티켓에서 인계 수를 센다.
        data["policy"] = voice_queries.policy()
        data["chat_policy"] = ai_chat.policy()
        data["openai"] = openai_settings.admin_settings()
        data["voice_counts"] = report_queries.voice_counts_since(midnight(today()))
    elif tab == "quotas":
        # 목표 편집 링크는 계정 탭의 동일한 편집 폼으로 연결한다.
        data["people"] = quota_rows()
        period = request.args.get("period")
        data["period"] = period if period in dict(PERIODS) else "daily"
        selected = request.args.get("edit")
        if selected:
            return redirect(url_for("management.index", tab="agents", filter="all", edit=selected))
    elif tab in ("ai-calls", "voice-recordings"):
        tab = "ai-calls"
        data["clips"] = call_list()
        data["rkx_calls"] = voice_queries.rkx_calls()
    elif tab == "ai-chats":
        page = max(1, min(request.args.get("page", 1, type=int), 2**31 - 1))
        sessions = ai_chat.sessions(41, (page - 1) * 40)
        selected = request.args.get("chat")
        chat = ai_chat.session(uuid_field(selected)) if selected else None
        turn_page = max(1, min(request.args.get("turn_page", 1, type=int), 2**31 - 1))
        turns = ai_chat.turns(chat["chat_id"], 41, (turn_page - 1) * 40) if chat else []
        data.update(ai_sessions=sessions[:40], page=page, more=len(sessions) > 40, ai_detail=chat,
                    turns=list(reversed(turns[:40])), turn_page=turn_page, older=len(turns) > 40)
    elif tab == "audit":
        # 한 페이지에 40건을 보여 준다. 41번째 행까지 조회해 다음 페이지 존재 여부를 정한다.
        page = max(1, min(request.args.get("page", 1, type=int), 2**31 - 1))
        changes = report_queries.admin_changes((page - 1) * 40)
        data.update(changes=changes[:40], page=page, more=len(changes) > 40)
    else:
        tab = tab if tab in ("history", "calls", "chats") else "overview"
        cases = case_queries.desk_cases(include_recordings=tab == "history")
        items = [dict(case_info(c), login_id=c["login_id"], display_name=c["display_name"], recording_count=c.get("recording_count", 0),
                      content_visible=content_visible(c), editable_recording=c.get("editable_recording")) for c in cases]
        if tab != "history":
            # 선택 채널에 속하는 티켓으로 상태별 건수를 먼저 계산한다. 상태 필터는 표시 목록에만 적용해
            # 한 상태를 조회해도 다른 상태의 건수가 화면에 함께 남도록 한다.
            kind = request.args.get("channel")
            if tab == "overview":
                data.update(dashboard_charts(kind))
                data["in_progress"] = Counter(i["current_agent_id"] for i in items if i["status"] == "IN_PROGRESS")
            else:
                kind = "PHONE" if tab == "calls" else "WEB"
                data["metrics"] = channel_metrics(kind)
            scoped = [i for i in items if kind not in ("WEB", "PHONE") or i["channel"] == kind]
            counts = Counter(i["status"] for i in scoped)
            data.update(team=quota_rows(), counts=counts, channel_count=len(scoped), selected=kind)
            if tab == "overview":
                breakdown = Counter((i["status"], i["channel"]) for i in scoped)
                breakdown.update(("ALL", i["channel"]) for i in scoped)
                data["breakdown"] = breakdown
            else:
                status = request.args.get("status", "ALL")
                if status not in ("QUEUED", "IN_PROGRESS", "CLOSED"):
                    status = "ALL"
                tickets = [i for i in scoped if status == "ALL" or i["status"] == status]
                page_size = 6
                pages = max(1, (len(tickets) + page_size - 1) // page_size)
                page = max(1, min(request.args.get("page", 1, type=int), pages))
                start = (page - 1) * page_size
                data.update(items=tickets[start:start + page_size], ticket_count=len(tickets), status_filter=status,
                            page=page, pages=pages, page_query={"tab": tab, "status": status})
        else:
            selected = request.args.get("channel")
            selected = selected if selected in ("WEB", "PHONE", "RECORDINGS") else "ALL"
            data.update(selected=selected, items=[i for i in items if selected == "ALL" or i["channel"] == selected
                                                 or (selected == "RECORDINGS" and i["recording_count"])])
            notes = staff_notes()
            # 새 메모 폼에는 재시도용 요청 ID와 보관 기한 안의 티켓을 전달한다.
            # 이미 삭제되거나 만료된 메모 편집 링크는 이력 목록으로 돌려보낸다.
            data.update(notes=notes, note_editor=None)
            edit = request.args.get("note")
            if edit == "new":
                data.update(note_editor={}, note_request_id=new_id(),
                            note_tickets=[dict(case_info(c), login_id=c["login_id"]) for c in cases if content_visible(c)])
            elif edit:
                data["note_editor"] = next((n for n in notes if str(n["note_id"]) == edit), None)
                if data["note_editor"] is None:
                    return redirect(url_for("management.index", tab="history", channel=selected))
        g.audit_target = ("CASE_LIST", None, None)
    if tab in ("overview", "ai-calls", "ai-chats"):
        scope = {"ai-calls": "VOICE", "ai-chats": "CHAT"}.get(tab)
        data["usage"] = usage_report(request.args.get("usage_period"), scope)
        data["usage_query"] = {key: request.args[key] for key in ("channel", "unit", "day", "month", "ai_view", "page", "chat", "turn_page") if key in request.args}
    return render_template("admin.html", tab=tab, user=g.admin_user, periods=PERIODS, **data)


@management.post("/agents", defaults={"staff_id": None})
@management.post("/agents/<uuid:staff_id>")
def save_agent(staff_id):
    # 두 POST 주소를 한 함수가 받는다. 경로에 직원 ID가 있으면 잠근 계정을 수정하고,
    # 없으면 비밀번호가 있는 새 상담원을 생성한다. 수정 폼의 빈 비밀번호는 변경 생략을 뜻한다.
    person = agent_account(staff_id, lock=True) if staff_id else None
    data = payload()
    login = text_field(data.get("login_id"), 80)
    name = text_field(data["display_name"], 60) if data.get("display_name") else None
    password = data.get("password", "")
    if not isinstance(password, str) or len(password) > 512 or (not person and not password):
        raise ApiError("INVALID_INPUT")
    try:
        if person:
            if "display_name" in data and person["display_name"] != name:
                staff_queries.set_display_name(staff_id, name)
                admin_change("상담원 이름 변경", login)
            if login != person["login_id"]:
                staff_queries.set_login(staff_id, login)
                admin_change("상담원 아이디 변경", person["login_id"] + " → " + login)
            if password:
                # 새 비밀번호의 해시를 저장하고 해당 직원의 세션을 모두 종료해 다시 로그인하도록 한다.
                staff_queries.set_password(staff_id, generate_password_hash(password))
                staff_queries.revoke_sessions(staff_id)
                admin_change("비밀번호 재설정", login)
        else:
            staff_id = new_id()
            staff_queries.create_agent(staff_id, login, name, generate_password_hash(password))
            admin_change("상담원 계정 생성", login)
    except IntegrityError as error:
        if error.args[0] == 1062:
            raise ApiError("LOGIN_ID_TAKEN") from None
        raise
    # 계정 폼에서 목표도 함께 보내면 같은 요청에서 검증·저장한다.
    if any(key in data for key, _ in PERIODS):
        write_quota(staff_id, login, data)
    return {"staff_id": staff_id}, 200 if person else 201


@management.post("/agents/<uuid:staff_id>/status")
def account_status(staff_id):
    # 계정의 실제 상태가 달라질 때만 반영한다. 비활성화할 때는 세션을 폐기하고
    # 열려 있는 담당 티켓만 대기열로 돌려보낸다. 종결 건의 담당자는 이력에 남는다.
    person = agent_account(staff_id, lock=True)
    active = payload().get("status")
    if active not in ("active", "inactive"):
        raise ApiError("INVALID_INPUT")
    enabled = active == "active"
    if enabled != bool(person["is_active"]):
        staff_queries.set_active(staff_id, enabled)
        if not enabled:
            staff_queries.revoke_sessions(staff_id)
            opened = case_queries.lock_open_assignments(staff_id)
            for case in opened:
                set_assignee(case, None, g.actor)
                admin_change("상담 배정 해제", person["login_id"], case["case_id"])
        admin_change("계정 활성화" if enabled else "계정 비활성화", person["login_id"])
    return {"active": enabled}


@management.post("/agents/<uuid:staff_id>/quota")
def save_quota(staff_id):
    # 목표 전용 API도 계정 폼과 같은 검증·저장 함수를 사용해 일·주·월 기준을 일치시킨다.
    person = agent_account(staff_id, lock=True)
    write_quota(staff_id, person["login_id"], payload())
    return {"staff_id": staff_id}


def quota_values(data, maximum=99999):
    # 빈 값은 직접 지정한 목표가 없다는 뜻이고, 숫자 0은 목표 0건이다.
    goals = []
    for key, _ in PERIODS:
        if key not in data:
            raise ApiError("INVALID_INPUT")
        value = data[key]
        if value is None or value == "":
            goals.append(None)
        elif str(value).isascii() and str(value).isdecimal() and len(str(value)) <= len(str(maximum)) and int(value) <= maximum:
            goals.append(int(value))
        else:
            raise ApiError("INVALID_INPUT")
    return goals


def write_quota(staff_id, login, data):
    goals = quota_values(data)
    previous = staff_queries.quota(staff_id) or {}
    # 직전 목표와 같으면 쓰기와 이력 생성을 생략한다.
    if goals != [previous.get(key) for key, _ in PERIODS]:
        staff_queries.set_quota(staff_id, *goals)
        summary = " / ".join("미설정" if goal is None else str(goal) for goal in goals)
        admin_change("할당량 변경", f"{login} · 일/주/월 {summary}")


@management.post("/notices")
def publish_notice():
    # 클라이언트가 만든 notice_id로 중복 게시를 구별한다. 같은 관리자가 같은 제목·본문으로
    # 재시도하면 기존 ID를 반환하고, 동일 ID에 다른 내용이 들어오면 충돌로 응답한다.
    data = payload()
    notice_id = uuid_field(data.get("notice_id"))
    title, body = text_field(data.get("title"), 100), text_field(data.get("body"), 3000)
    try:
        staff_queries.publish_notice(notice_id, g.actor, title, body)
    except IntegrityError as error:
        if error.args[0] != 1062:
            raise
        old = staff_queries.notice_by_id(notice_id)
        if old != dict(author_id=g.actor,title=title,body=body):
            raise ApiError("REQUEST_KEY_CONFLICT") from None
        return {"notice_id": notice_id}
    admin_change("공지 게시", title)
    return {"notice_id": notice_id}, 201


@management.post("/teams", defaults={"team_id": None})
@management.post("/teams/<uuid:team_id>")
def save_team(team_id):
    # members는 저장할 팀원 목록, previous는 화면을 열었을 때의 직원별 소속 팀이다.
    # 기존 팀을 수정할 때는 팀 이름도 화면에서 본 값과 일치하는지 잠금 상태에서 확인한다.
    data = payload()
    name = text_field(data.get("name"), 40)
    members, previous = data.get("members"), data.get("previous")
    if not isinstance(members, list) or not isinstance(previous, dict):
        raise ApiError("INVALID_INPUT")
    chosen = {uuid_field(value) for value in members}
    goals = quota_values(data, maximum=4294967295) if any(key in data for key, _ in PERIODS) else None
    new = team_id is None
    team_id = team_id or new_id()
    if not new:
        team = staff_queries.lock_team(team_id)
        if not team or team["name"] != data.get("previous_name"):
            raise ApiError("TEAM_CHANGED")
        stored_goals = [team[key] for key, _ in PERIODS]
        if goals is not None and goals != stored_goals and data.get("previous_goals") != stored_goals:
            raise ApiError("TEAM_CHANGED")
    try:
        if new:
            staff_queries.create_team(team_id, name)
        else:
            staff_queries.rename_team(team_id, name)
    except IntegrityError as error:
        if error.args[0] == 1062:
            raise ApiError("TEAM_NAME_TAKEN") from None
        raise
    current = {a["staff_id"]: a["team_id"] for a in staff_queries.lock_team_memberships()}
    # 이 팀의 기존 명단이 달라졌거나 선택한 상담원 계정이 사라졌으면 갱신을 중단한다.
    original = {key for key, value in previous.items() if value == team_id}
    if not chosen.issubset(current) or {key for key, value in current.items() if value == team_id} != original:
        raise ApiError("TEAM_CHANGED")
    for key in sorted(original ^ chosen):
        # 기존·새 명단 중 한쪽에만 있는 직원이 이동 대상이다. 다른 팀에서 누군가 먼저 옮겼다면
        # 화면의 이전 소속값이 DB와 다르므로 그 변경을 덮어쓰지 않는다.
        if key not in previous or previous[key] != current[key]:
            raise ApiError("TEAM_CHANGED")
        staff_queries.set_team(key, team_id if key in chosen else None)
    if goals is not None:
        staff_queries.set_team_quota(team_id, *goals)
    admin_change("팀 생성" if new else "팀 구성 수정", name)
    return {"team_id": team_id}, 201 if new else 200


@management.post("/openai-settings/chat-models")
def openai_chat_models():
    return {"models": openai_settings.chat_models(payload())}


@management.post("/openai-settings")
def save_openai_settings():
    data = payload() if request.is_json else request.form
    openai_settings.save_settings(data)
    admin_change("OpenAI 설정 변경", "API 키·모델" if data.get("api_key", "").strip() else "상담 모델")
    if request.is_json:
        return {"saved": True}
    return redirect(url_for("management.index", tab="ai", _anchor="openai-settings"), code=303)


@management.post("/ai-settings")
def save_ai_policy():
    data = payload()
    enabled, offer, instructions = data.get("enabled"), data.get("offer_handoff"), data.get("instructions")
    if not isinstance(enabled, bool) or not isinstance(offer, bool) or not isinstance(instructions, str) or len(instructions) > 600:
        raise ApiError("INVALID_INPUT")
    greeting = text_field(data["greeting"], 200) if "greeting" in data else None
    if "ringback_enabled" in data and not isinstance(data["ringback_enabled"], bool):
        raise ApiError("INVALID_INPUT")
    voice_queries.update_policy(enabled, offer, instructions.strip(), greeting, data.get("ringback_enabled"))
    admin_change("AI 응대 설정 변경", "AI 음성 상담")
    return {"enabled": enabled}


@management.post("/ai-chat-settings")
def save_ai_chat_policy():
    data = payload()
    enabled, style = data.get("enabled"), data.get("reply_style")
    if not isinstance(enabled, bool) or style not in ("brief", "steps"):
        raise ApiError("INVALID_INPUT")
    ai_chat.update_policy(enabled, text_field(data.get("greeting"), 200), style)
    admin_change("AI 응대 설정 변경", "AI 채팅 상담")
    return {"enabled": enabled}


@management.get("/cases/<uuid:case_id>")
def history(case_id):
    # 상담 본문의 보관 기한이 지나도 관리자는 보관 중인 녹취를 관리할 수 있다.
    case = case_row(case_id)
    g.audit_target = ("CASE_VIEW", case_id, None)
    visible = content_visible(case)
    data = {}
    if case["channel"] == "WEB":
        messages = chat_queries.messages(case_id) if visible else []
        data["messages"] = [message_info(m) for m in messages]
    else:
        data["phone"] = phone_info(case_id) if visible else None
        data["recordings"] = recording_queries.case_history(case_id)
    channel_tab = "chats" if case["channel"] == "WEB" else "calls"
    channel_view = request.args.get("tab") == channel_tab
    mode = request.args.get("mode", "view" if channel_view else "edit")
    context = ({"tab": channel_tab, "page": request.args.get("page", type=int), "status": request.args.get("status")}
               if channel_view else {"channel": request.args.get("channel", case["channel"])})
    assignee = staff_queries.agent_account(case["current_agent_id"]) if case["current_agent_id"] else None
    return render_template("admin.html", tab=channel_tab if channel_view else "history", channel_view=channel_view,
                           editing=mode == "edit", case_query=context, assignee=assignee,
                           user=g.admin_user, detail=case_info(case), assignments=assignment_log(case_id),
                           visible=visible, notes=staff_notes(case_id) if visible else [], result=case["result_note"] if visible else None, **data)


def editable_history(case_id, expected=None):
    # 관리자 정정은 종결 후에도 본문 보관 기한 안이면 가능하다. 티켓을 잠그고
    # 채널 전용 기능이면 채널까지 확인한 뒤 편집할 행을 돌려준다.
    case = case_row(case_id, lock=True)
    if expected:
        channel(case, expected)
    if not content_visible(case):
        raise ApiError("CONTENT_EXPIRED")
    return case


@management.post("/cases/<uuid:case_id>/category")
def edit_category(case_id):
    case = case_row(case_id, lock=True)
    data = payload()
    category = data.get("inquiry_type")
    if not isinstance(category, str) or (category and category not in TOPICS):
        raise ApiError("INVALID_INPUT")
    if (category or None) != case["inquiry_type"]:
        if data.get("previous_category", case["inquiry_type"] or "") != (case["inquiry_type"] or ""):
            raise ApiError("CASE_CATEGORY_CHANGED")
        case_queries.set_category(case_id, category or None)
        admin_change("문의 유형 수정", TOPICS.get(category, "미분류"), case_id)
    return {"inquiry_type": category or None}


@management.post("/notes")
def create_note():
    # 메모 생성 키의 범위는 티켓 한 건이다. 같은 요청을 다시 보내면 작성자·본문을 비교해
    # 처음 생성한 note_id를 그대로 반환하고, 다른 내용이면 요청 키 충돌로 처리한다.
    data = payload()
    case_id = uuid_field(data.get("case_id"))
    request_id = uuid_field(data.get("request_id"))
    body = text_field(data.get("body"))
    editable_history(case_id)
    existing = case_queries.note_by_request(case_id, request_id)
    if existing:
        if existing["created_by"] != g.actor or existing["body"] != body:
            raise ApiError("REQUEST_KEY_CONFLICT")
        return {"note_id": existing["note_id"]}
    note_id = case_queries.add_note(case_id, request_id, g.actor, body)
    admin_change("직원용 기록 추가", f"기록 #{note_id}", case_id)
    return {"note_id": note_id}, 201


@management.post("/cases/<uuid:case_id>/notes/<int:note_id>")
def change_note(case_id, note_id):
    # 메모 변경 내용이 기존 값과 다를 때만 클라이언트가 본 updated_at과 DB의 최종 수정 시각을 비교한다.
    # 다른 관리자가 먼저 고친 경우에는 새 내용을 덮지 않고 새로고침할 수 있도록 오류를 반환한다.
    editable_history(case_id)
    data = payload()
    note = case_queries.note_for_edit(case_id, note_id)
    delete = data.get("operation") == "delete"
    if not note or (note["body"] is None and not delete):
        raise ApiError("STAFF_NOTE_NOT_FOUND")
    body = None if delete else text_field(data.get("body"))
    if note["body"] != body:
        if data.get("updated_at") != stamp(note["updated_at"]):
            raise ApiError("STAFF_NOTE_CHANGED")
        # 삭제는 본문만 NULL로 만든다. 생성 요청 ID가 남아 지연된 재시도가 메모를 다시 만들 수 없다.
        case_queries.update_note(case_id, note_id, body)
        admin_change("직원용 기록 삭제" if delete else "직원용 기록 수정", f"기록 #{note_id}", case_id)
    return {"note_id": note_id}


@management.post("/cases/<uuid:case_id>/messages/<int:message_id>")
def edit_message(case_id, message_id):
    # 보관 중인 웹 티켓에서 본문이 남은 메시지만 정정할 수 있다. 메시지 ID와 티켓 ID를 함께 확인한다.
    editable_history(case_id, "WEB")
    message = chat_queries.message_for_edit(case_id, message_id)
    if not message:
        raise ApiError("MESSAGE_NOT_FOUND")
    data = payload()
    if data.get("operation") == "delete":
        # 빈 본문은 삭제 표시다. 보관 기한 정리까지 전송 요청 ID를 남겨 재전송으로 본문이 복구되는 것을 막는다.
        chat_queries.clear_message(case_id, message_id)
        admin_change("메시지 삭제", f"메시지 #{message_id}", case_id)
    else:
        body = text_field(data.get("body"))
        if body != message["body"]:
            chat_queries.update_message(case_id, message_id, body)
            admin_change("메시지 수정", f"메시지 #{message_id}", case_id)
    return {"message_id": str(message_id)}


@management.post("/cases/<uuid:case_id>/history")
def edit_history(case_id):
    # 티켓 전체 삭제와 상담 내용만 지우는 요청을 구분한다. 내용 삭제는 티켓 자체와 배정 이력을 보존한다.
    data = payload()
    if data.get("operation") == "delete_ticket":
        return delete_ticket(case_id)
    case = editable_history(case_id)
    if data.get("operation") == "delete_phone_response":
        channel(case, "PHONE")
        save_phone_response(case_id, dict.fromkeys(PHONE_RECORD_FIELDS), allow_clear=True)
        admin_change("전화 응대 기록 삭제", "통화 일시·구분·시간 및 응대 내용", case_id)
    elif data.get("operation") == "delete":
        if case["channel"] == "WEB":
            chat_queries.clear_messages(case_id)
        else:
            voice_queries.clear_phone_response(case_id)
            recording_queries.mark_case_for_deletion(case_id)
        case_queries.clear_notes(case_id)
        case_queries.clear_result(case_id)
        admin_change("상담 기록 삭제", "웹채팅" if case["channel"] == "WEB" else "전화 응대 및 녹취", case_id)
    else:
        # 전화 응대는 요청에 들어 있는 필드만 수정하거나 비운다. 결과 메모는 종결된 티켓에서 수정한다.
        fields = {key: data[key] for key in (*PHONE_RECORD_FIELDS, "consent_state") if key in data}
        if fields:
            channel(case, "PHONE")
            save_phone_response(case_id, fields, allow_clear=True)
        if "result_note" in data and case["closed_at"]:
            outcome = data.get("outcome", case["outcome"])
            if outcome is not None and (not isinstance(outcome, str) or (outcome and outcome not in OUTCOMES)):
                raise ApiError("INVALID_INPUT")
            case_queries.update_result(case_id, text_field(data["result_note"]), outcome or None)
        elif not fields:
            raise ApiError("INVALID_INPUT")
        admin_change("상담 기록 수정", "웹채팅" if case["channel"] == "WEB" else "전화 응대", case_id)
    return {"case_id": case_id}


def delete_ticket(case_id):
    # 삭제된 행도 조회할 수 있는 원본 테이블을 잠근다. 이미 처리한 삭제의 재요청에도 204를 돌려준다.
    case = case_queries.lock_for_deletion(case_id)
    if not case:
        raise ApiError("CASE_NOT_FOUND")
    if case["deleted_at"] is None:
        # 본문과 입장 수단을 지우고 녹취는 정리 작업에 맡긴다. 삭제 표시된 티켓은 visible_case에서 제외된다.
        purge_case_content(case_id)
        chat_queries.revoke_entry_codes(case_id)
        chat_queries.delete_case_sessions(case_id)
        recording_queries.mark_case_for_deletion(case_id)
        # 감사 이력과 뒤늦은 접수 재시도를 구분하는 데 필요한 티켓 ID와 접수 요청 ID는 보존한다.
        case_queries.mark_deleted(case_id)
        admin_change("상담 티켓 삭제", "웹채팅" if case["channel"] == "WEB" else "전화", case_id)
    return "", 204


@management.get("/voice-recordings/<uuid:recording_id>/audio")
def play_voice(recording_id):
    # 직원 업로더가 없는 AI 녹취를 찾는다. 재생 가능 여부는 공통 스트리밍 함수에서 확인한다.
    item = recording_queries.lock_voice_recording(recording_id)
    g.audit_target = ("RECORDING_PLAY", item["case_id"] if item else None, recording_id)
    if not item:
        raise ApiError("RECORDING_NOT_FOUND")
    return stream_recording(item)


@management.get("/cases/<uuid:case_id>/recordings/<uuid:recording_id>/content")
def play_case_recording(case_id, recording_id):
    g.audit_target = ("RECORDING_PLAY", case_id, recording_id)
    channel(case_row(case_id, lock=True), "PHONE")
    item = recording_queries.lock_case_recording(case_id, recording_id)
    if not item:
        raise ApiError("RECORDING_NOT_FOUND")
    return stream_recording(item)


@management.post("/voice-recordings/<uuid:recording_id>/summary")
def request_voice_summary(recording_id):
    # 재생 가능한 AI 녹취에 요약 작업을 예약한다. 아직 요청하지 않았거나 이전 시도가
    # 실패한 경우만 대기열에 넣어, 이미 처리 중이거나 완성된 결과를 중복 실행하지 않는다.
    item = recording_queries.lock_summary_source(recording_id)
    if not item or item["deleted_at"]:
        raise ApiError("RECORDING_NOT_FOUND")
    check_not_deleted(item)
    if item["state"] != "READY":
        raise ApiError("RECORDING_NOT_READY")
    previous = voice_queries.summary_state(recording_id)
    state = previous["state"] if previous else None
    if state in (None, "FAILED"):
        voice_queries.queue_summary(recording_id)
        admin_change("AI 통화 요약 요청", recording_id, item["case_id"])
        state = "QUEUED"
    if not request.is_json:
        # 폼에서는 해당 녹취 위치로 돌아간다. JSON은 완료 시 200, 대기·처리 중이면 202와 상태를 받는다.
        return redirect(url_for("management.index", tab="ai-calls", _anchor="clip-" + recording_id), code=303)
    return {"state": state}, 200 if state == "READY" else 202


@management.post("/voice-recordings/<uuid:recording_id>", defaults={"case_id": None})
@management.post("/cases/<uuid:case_id>/recordings/<uuid:recording_id>")
def edit_recording(case_id, recording_id):
    # 티켓 상세에서 온 요청은 전화 티켓과 녹취의 관계를, AI 통화 목록에서 온 요청은
    # 직원 업로더가 없는 녹취인지 확인한다. 이후의 표시명 변경과 삭제 예약은 같은 절차를 쓴다.
    if case_id:
        channel(case_row(case_id, lock=True), "PHONE")
        item = recording_queries.lock_case_recording(case_id, recording_id)
    else:
        item = recording_queries.lock_voice_recording(recording_id)
    if not item:
        raise ApiError("RECORDING_NOT_FOUND")
    case_id = item["case_id"]
    data = payload()
    if data.get("operation") == "delete":
        if item["state"] not in ("DELETE_PENDING", "DESTROYED"):
            # 여기서는 상태를 바꿔 재생을 중단시킨다. 파일 제거와 파기 시각 기록은 purge가 맡는다.
            recording_queries.mark_for_deletion(recording_id)
            admin_change("녹취 삭제 요청", recording_id, case_id)
    else:
        check_not_deleted(item)
        label = data.get("label")
        if not isinstance(label, str) or len(label) > 120:
            raise ApiError("INVALID_INPUT")
        recording_queries.set_label(recording_id, label.strip())
        admin_change("녹취 표시명 수정", recording_id, case_id)
    return {"recording_id": recording_id}
