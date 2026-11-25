from collections import Counter
from datetime import timedelta
from pathlib import Path

from flask import Blueprint, g, redirect, render_template, request, url_for
from pymysql.err import IntegrityError
from werkzeug.security import generate_password_hash

from .cases import assignment_log, message_info, phone_info, save_phone_response, set_assignee, staff_notes
from .common import ApiError, admin, case_info, case_row, channel, content_visible, payload, staff_user, text_field, uuid_field
from .db import admin_change, db, new_id, one, rows, run, stamp, utcnow
from .desk_reports import channel_metrics, dashboard_charts, midnight, notices, report_range, statistics, today
from .recordings import check_expiry, purge_case_content, stream_recording
from .voice_summaries import call_list

# 이 Blueprint의 화면과 변경 API는 모두 /admin 아래에 등록되고 require_admin 검사를 거친다.
management = Blueprint("management", __name__, url_prefix="/admin")
PERIODS = (("daily", "일간"), ("weekly", "주간"), ("monthly", "월간"))


def init_admin():
    """Upgrade existing demo volumes in place and initialize fresh installations."""
    # 이미 데이터가 있는 DB에도 적용하는 Flask CLI 명령이다. visible_case 뷰에서 참조할
    # 삭제 표시 열을 먼저 만든 뒤 관리자용 뷰와 테이블을 준비한다.
    if not one("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() "
               "AND table_name='support_case' AND column_name='deleted_at'"):
        run("ALTER TABLE support_case ADD COLUMN deleted_at DATETIME(6)")
    sql = Path(__file__).with_name("admin_schema.sql").read_text(encoding="utf-8")
    for statement in sql.split(";"):
        if statement.strip():
            run(statement)
    # 배정 해제 이력에서는 새 담당자가, 채팅 접수 전 방문 세션에서는 티켓이 아직 없을 수 있다.
    # 기존 설치의 두 외래 키 열이 이 상태를 NULL로 표현할 수 있게 한다.
    column = one(
        "SELECT IS_NULLABLE AS nullable FROM information_schema.columns WHERE table_schema=DATABASE() "
        "AND table_name='assignment_history' AND column_name='new_agent_id'"
    )
    if column["nullable"] == "NO":
        run("ALTER TABLE assignment_history MODIFY new_agent_id CHAR(36) NULL")
    column = one("SELECT IS_NULLABLE AS nullable FROM information_schema.columns WHERE table_schema=DATABASE() "
                 "AND table_name='customer_session' AND column_name='case_id'")
    if column["nullable"] == "NO":
        run("ALTER TABLE customer_session MODIFY case_id CHAR(36) NULL")
    # AI 통화는 직원 업로더가 없고 상담원 인계 전에는 티켓도 없으므로 두 열이 NULL을 허용해야 한다.
    if one("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='recording' "
           "AND column_name IN ('case_id','uploaded_by') AND is_nullable='NO' LIMIT 1"):
        run("ALTER TABLE recording MODIFY case_id CHAR(36) NULL, MODIFY uploaded_by CHAR(36) NULL")
    migrate_recording_labels()
    migrate_desk()


def migrate_desk():
    """Apply the additive portal migration, including after a partial DDL run."""
    # MySQL의 스키마 변경은 중간까지 적용된 채 끝날 수 있다. 현재 열 목록을 읽고
    # desk_schema.sql의 ADD COLUMN 중 이미 적용한 항목을 건너뛰어 재실행할 수 있게 한다.
    columns = {(row["tbl"], row["col"]) for row in rows(
        "SELECT table_name AS tbl,column_name AS col FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name IN ('staff_account','support_case')")}
    for chunk in Path(__file__).with_name("desk_schema.sql").read_text(encoding="utf-8").split(";"):
        statement = chunk.strip()
        if not statement:
            continue
        if statement.startswith("ALTER TABLE"):
            tokens = statement.split()
            if (tokens[2], tokens[5]) in columns:
                continue
        run(statement)
    db().commit()


def migrate_recording_labels():
    # 과거 별도 테이블에 저장한 녹취 표시명을 recording.label로 옮긴다.
    # 해당 테이블이 없는 새 설치나 마이그레이션 완료 상태에서는 대상 열만 확인하면 된다.
    if not one("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() "
               "AND table_name='recording' AND column_name='label'"):
        run("ALTER TABLE recording ADD COLUMN label VARCHAR(120)")
    if not one("SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() "
               "AND table_name='recording_label'"):
        return

    # 복사 중 어느 쪽의 이름도 바뀌지 않도록 두 테이블을 잠근다. NULL까지 고려해 바이트 단위로
    # 일치하는지 확인하고, 차이가 있으면 예외를 내서 원본 테이블을 보존한다.
    run("LOCK TABLES recording WRITE, recording_label WRITE")
    try:
        run("UPDATE recording JOIN recording_label USING (recording_id) "
            "SET recording.label=recording_label.label WHERE recording.label IS NULL")
        if one("SELECT 1 FROM recording_label LEFT JOIN recording USING (recording_id) "
               "WHERE NOT (CAST(recording.label AS BINARY) <=> CAST(recording_label.label AS BINARY)) LIMIT 1"):
            raise RuntimeError("녹취 표시명이 일치하지 않아 기존 recording_label 테이블을 보존했습니다.")
        run("DROP TABLE recording_label")
    except Exception:
        db().rollback()
        raise
    finally:
        run("UNLOCK TABLES")


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
    # 일간은 오늘, 주간은 이번 주 월요일, 월간은 이달 1일의 한국 자정부터 센다.
    # DB의 시각이 UTC이므로 기간 경계에서 9시간을 빼고 SQL의 비교값으로 사용한다.
    kst = timedelta(hours=9)
    today = (utcnow() + kst).replace(hour=0, minute=0, second=0, microsecond=0)
    starts = (today - kst, today - timedelta(days=today.weekday()) - kst, today.replace(day=1) - kst)
    people = rows(
        "SELECT a.staff_id, a.login_id, a.display_name, a.is_active, q.daily, q.weekly, q.monthly FROM staff_account a "
        "LEFT JOIN agent_quota q ON q.staff_id=a.staff_id WHERE a.role='AGENT' "
        + ("AND a.staff_id=%s " if staff_id else "") + "ORDER BY a.is_active DESC, a.login_id",
        (staff_id,) if staff_id else (),
    )
    for table, owner, at, metric in (
        ("assignment_history JOIN visible_case USING (case_id)", "new_agent_id", "changed_at", "assigned"),
        ("visible_case", "current_agent_id", "closed_at", "completed"),
    ):
        # 배정 건수는 배정 이력의 새 담당자 기준, 종결 건수는 티켓의 현재 담당자 기준이다.
        # 같은 티켓이 한 기간에 여러 번 배정돼도 상담원별로 한 건만 세고 실적이 없으면 0을 채운다.
        counts = ", ".join(f"COUNT(DISTINCT IF({at}>=%s, case_id, NULL)) AS {key}" for key, _ in PERIODS)
        records = rows(
            f"SELECT {owner} AS staff_id, {counts} FROM {table} WHERE {at}>=%s "
            + (f"AND {owner}=%s " if staff_id else "") + f"GROUP BY {owner}",
            (*starts, min(starts), *((staff_id,) if staff_id else ())),
        )
        by_staff = {row["staff_id"]: row for row in records}
        for person in people:
            person[metric] = {key: by_staff.get(person["staff_id"], {}).get(key, 0) for key, _ in PERIODS}
    return people


def agent_account(staff_id, lock=False):
    # 관리자 계정은 상담원 편집 대상에서 제외한다. 수정 요청은 계정 행을 잠근 상태로 검사를 이어간다.
    person = one(
        "SELECT staff_id,login_id,display_name,is_active FROM staff_account WHERE staff_id=%s AND role='AGENT'"
        + (" FOR UPDATE" if lock else ""), (staff_id,),
    )
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
    elif tab == "notices":
        data.update(notices=notices(), notice_id=new_id())
    elif tab == "org":
        # 수정 폼에 현재 직원별 팀 배정표(snapshot)를 전달한다. 저장할 때 이 값과 DB를 비교한다.
        teams = rows("SELECT team_id,name FROM staff_team ORDER BY name")
        members = rows("SELECT staff_id,login_id,display_name,team_id,is_active FROM staff_account WHERE role='AGENT' ORDER BY login_id")
        selected = request.args.get("team", teams[0]["team_id"] if teams else None)
        team = next((t for t in teams if t["team_id"] == selected), None)
        if selected and not team:
            raise ApiError("INVALID_INPUT")
        dates = report_range()
        data.update(teams=teams, members=members, team=team, dates=dates, snapshot={p["staff_id"]:p["team_id"] for p in members},
                    report=statistics(dates, team_id=selected) if team else None)
    elif tab == "ai":
        # 한국 시간 오늘의 음성 동의·거부 이벤트에서 통화 수를, 같은 접수 키의 티켓에서 인계 수를 센다.
        data["policy"] = one("SELECT * FROM voice_policy WHERE policy_id=1")
        data["voice_counts"] = one("SELECT COUNT(DISTINCT a.recording_id) AS sessions, COUNT(DISTINCT c.case_id) AS handed "
                                   "FROM audit_event a LEFT JOIN visible_case c ON c.intake_request_id=a.recording_id "
                                   "WHERE a.action IN ('VOICE_RECORDING_AGREED','VOICE_RECORDING_REFUSED') AND a.created_at>=%s", (midnight(today()),))
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
    elif tab == "audit":
        # 한 페이지에 40건을 보여 준다. 41번째 행까지 조회해 다음 페이지 존재 여부를 정한다.
        page = max(1, min(request.args.get("page", 1, type=int), 2**31 - 1))
        changes = rows(
            "SELECT h.action,h.target,h.case_id,h.created_at,s.login_id,c.deleted_at FROM admin_change h "
            "JOIN staff_account s ON s.staff_id=h.actor_id LEFT JOIN support_case c ON c.case_id=h.case_id "
            "ORDER BY h.change_id DESC LIMIT 41 OFFSET %s",
            ((page - 1) * 40,),
        )
        data.update(changes=changes[:40], page=page, more=len(changes) > 40)
    else:
        tab = tab if tab in ("history", "calls", "chats") else "overview"
        recordings = ""
        if tab == "history":
            # 이력 화면만 녹취 건수와 편집 가능한 녹취의 존재 여부가 필요하다.
            recordings = (",(SELECT COUNT(*) FROM recording r WHERE r.case_id=c.case_id) AS recording_count, "
                          "EXISTS(SELECT 1 FROM recording r WHERE r.case_id=c.case_id AND r.expires_at>UTC_TIMESTAMP(6) "
                          "AND r.state NOT IN ('DELETE_PENDING','DESTROYED')) AS editable_recording")
        cases = rows(
            "SELECT c.case_id,c.channel,c.opened_at,c.closed_at,c.content_expires_at,c.current_agent_id,c.inquiry_type,s.login_id,s.display_name" + recordings +
            " FROM visible_case c LEFT JOIN staff_account s ON s.staff_id=c.current_agent_id "
            "ORDER BY c.opened_at DESC,c.case_id"
        )
        items = [dict(case_info(c), login_id=c["login_id"], display_name=c["display_name"], recording_count=c.get("recording_count", 0),
                      content_visible=content_visible(c), editable_recording=c.get("editable_recording")) for c in cases]
        if tab != "history":
            # 선택 채널에 속하는 티켓으로 상태별 건수를 먼저 계산한다. 상태 필터는 표시 목록에만 적용해
            # 한 상태를 조회해도 다른 상태의 건수가 화면에 함께 남도록 한다.
            kind = request.args.get("channel")
            if tab == "overview":
                data.update(dashboard_charts(kind))
                data["team"] = quota_rows()
                data["in_progress"] = Counter(i["current_agent_id"] for i in items if i["status"] == "IN_PROGRESS")
            else:
                kind = "PHONE" if tab == "calls" else "WEB"
                data["metrics"] = channel_metrics(kind)
            scoped = [i for i in items if kind not in ("WEB", "PHONE") or i["channel"] == kind]
            counts = Counter(i["status"] for i in scoped)
            if tab == "overview":
                breakdown = Counter((i["status"], i["channel"]) for i in scoped)
                breakdown.update(("ALL", i["channel"]) for i in scoped)
                data["breakdown"] = breakdown
            status = request.args.get("status", "ALL")
            if status not in ("QUEUED", "IN_PROGRESS", "CLOSED"):
                status = "ALL"
            tickets = [i for i in scoped if status == "ALL" or i["status"] == status]
            # 여섯 건씩 페이지를 구성한다. 페이지 링크에는 현재 채널·상태·차트 기간을 유지한다.
            page_size = 6
            pages = max(1, (len(tickets) + page_size - 1) // page_size)
            page = max(1, min(request.args.get("page", 1, type=int), pages))
            start = (page - 1) * page_size
            query = {key: request.args[key] for key in ("tab", "channel", "status", "unit", "day", "month") if key in request.args}
            data.update(items=tickets[start:start + page_size], ticket_count=len(tickets), status_filter=status, counts=counts,
                        channel_count=len(scoped), selected=kind, page=page, pages=pages, page_query=query)
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
                run("UPDATE staff_account SET display_name=%s WHERE staff_id=%s", (name,staff_id))
                admin_change("상담원 이름 변경", login)
            if login != person["login_id"]:
                run("UPDATE staff_account SET login_id=%s WHERE staff_id=%s", (login, staff_id))
                admin_change("상담원 아이디 변경", person["login_id"] + " → " + login)
            if password:
                # 새 비밀번호의 해시를 저장하고 해당 직원의 세션을 모두 종료해 다시 로그인하도록 한다.
                run("UPDATE staff_account SET password_hash=%s WHERE staff_id=%s", (generate_password_hash(password), staff_id))
                run("DELETE FROM staff_session WHERE staff_id=%s", (staff_id,))
                admin_change("비밀번호 재설정", login)
        else:
            staff_id = new_id()
            run(
                "INSERT INTO staff_account (staff_id,login_id,display_name,password_hash,role) VALUES (%s,%s,%s,%s,'AGENT')",
                (staff_id, login, name, generate_password_hash(password)),
            )
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
        run("UPDATE staff_account SET is_active=%s WHERE staff_id=%s", (enabled, staff_id))
        if not enabled:
            run("DELETE FROM staff_session WHERE staff_id=%s", (staff_id,))
            opened = rows(
                "SELECT * FROM visible_case WHERE current_agent_id=%s AND closed_at IS NULL ORDER BY case_id FOR UPDATE",
                (staff_id,),
            )
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


def write_quota(staff_id, login, data):
    # 세 기간 모두 입력받는다. 빈 값은 목표 미설정(NULL), 숫자 0은 목표 0건이므로 구별해 저장한다.
    goals = []
    for key, _ in PERIODS:
        if key not in data:
            raise ApiError("INVALID_INPUT")
        value = data[key]
        if value is None or value == "":
            goals.append(None)
        elif str(value).isascii() and str(value).isdecimal() and len(str(value)) <= 5:
            goals.append(int(value))
        else:
            raise ApiError("INVALID_INPUT")
    previous = one("SELECT daily,weekly,monthly FROM agent_quota WHERE staff_id=%s", (staff_id,)) or {}
    # 직전 목표와 같으면 쓰기와 이력 생성을 생략한다.
    if goals != [previous.get(key) for key, _ in PERIODS]:
        run("REPLACE INTO agent_quota (staff_id,daily,weekly,monthly) VALUES (%s,%s,%s,%s)", (staff_id, *goals))
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
        run("INSERT INTO staff_notice (notice_id,author_id,title,body) VALUES (%s,%s,%s,%s)", (notice_id,g.actor,title,body))
    except IntegrityError as error:
        if error.args[0] != 1062:
            raise
        old = one("SELECT author_id,title,body FROM staff_notice WHERE notice_id=%s", (notice_id,))
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
    new = team_id is None
    team_id = team_id or new_id()
    if not new:
        team = one("SELECT name FROM staff_team WHERE team_id=%s FOR UPDATE", (team_id,))
        if not team or team["name"] != data.get("previous_name"):
            raise ApiError("TEAM_CHANGED")
    try:
        if new:
            run("INSERT INTO staff_team (team_id,name) VALUES (%s,%s)", (team_id,name))
        else:
            run("UPDATE staff_team SET name=%s WHERE team_id=%s", (name,team_id))
    except IntegrityError as error:
        if error.args[0] == 1062:
            raise ApiError("TEAM_NAME_TAKEN") from None
        raise
    current = {a["staff_id"]: a["team_id"] for a in rows(
        "SELECT staff_id,team_id FROM staff_account WHERE role='AGENT' ORDER BY staff_id FOR UPDATE")}
    # 이 팀의 기존 명단이 달라졌거나 선택한 상담원 계정이 사라졌으면 갱신을 중단한다.
    original = {key for key, value in previous.items() if value == team_id}
    if not chosen.issubset(current) or {key for key, value in current.items() if value == team_id} != original:
        raise ApiError("TEAM_CHANGED")
    for key in sorted(original ^ chosen):
        # 기존·새 명단 중 한쪽에만 있는 직원이 이동 대상이다. 다른 팀에서 누군가 먼저 옮겼다면
        # 화면의 이전 소속값이 DB와 다르므로 그 변경을 덮어쓰지 않는다.
        if key not in previous or previous[key] != current[key]:
            raise ApiError("TEAM_CHANGED")
        run("UPDATE staff_account SET team_id=%s WHERE staff_id=%s", (team_id if key in chosen else None,key))
    admin_change("팀 생성" if new else "팀 구성 수정", name)
    return {"team_id": team_id}, 201 if new else 200


@management.post("/ai-settings")
def save_ai_policy():
    # 음성 응대 사용 여부, 상담원 인계 제안 여부, 추가 응대 지침을 단일 운영 설정 행에 저장한다.
    data = payload()
    enabled, offer, instructions = data.get("enabled"), data.get("offer_handoff"), data.get("instructions")
    if not isinstance(enabled, bool) or not isinstance(offer, bool) or not isinstance(instructions, str) or len(instructions) > 600:
        raise ApiError("INVALID_INPUT")
    run("UPDATE voice_policy SET enabled=%s,offer_handoff=%s,instructions=%s WHERE policy_id=1", (enabled,offer,instructions.strip()))
    admin_change("AI 응대 설정 변경", "AI 음성 상담")
    return {"enabled": enabled}


@management.get("/cases/<uuid:case_id>")
def history(case_id):
    # 관리자 상세 화면을 채널별로 구성한다. 상담 본문·메모와 녹취는 각자의 보관 기한을 쓰므로
    # 본문을 읽을 수 있는지와 녹취 표시명을 보여 줄 수 있는지를 따로 판정한다.
    case = case_row(case_id)
    g.audit_target = ("CASE_VIEW", case_id, None)
    visible = content_visible(case)
    data = {}
    if case["channel"] == "WEB":
        messages = rows("SELECT * FROM chat_message WHERE case_id=%s AND body<>'' ORDER BY message_id", (case_id,)) if visible else []
        data["messages"] = [message_info(m) for m in messages]
    else:
        data["phone"] = phone_info(case_id) if visible else None
        data["recordings"] = rows(
            "SELECT r.recording_id,r.state,r.stored_at,r.expires_at,r.size_bytes,r.uploaded_by IS NULL AS ai,"
            "(r.expires_at>UTC_TIMESTAMP(6) AND r.state NOT IN ('DELETE_PENDING','DESTROYED')) AS editable,"
            "CASE WHEN r.expires_at>UTC_TIMESTAMP(6) AND r.state NOT IN ('DELETE_PENDING','DESTROYED') THEN r.label END AS label "
            "FROM recording r "
            "WHERE r.case_id=%s ORDER BY r.expires_at DESC,r.recording_id", (case_id,),
        )
    return render_template("admin.html", tab="history", user=g.admin_user, detail=case_info(case), assignments=assignment_log(case_id),
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


@management.post("/notes")
def create_note():
    # 메모 생성 키의 범위는 티켓 한 건이다. 같은 요청을 다시 보내면 작성자·본문을 비교해
    # 처음 생성한 note_id를 그대로 반환하고, 다른 내용이면 요청 키 충돌로 처리한다.
    data = payload()
    case_id = uuid_field(data.get("case_id"))
    request_id = uuid_field(data.get("request_id"))
    body = text_field(data.get("body"))
    editable_history(case_id)
    existing = one("SELECT note_id,created_by,body FROM staff_note WHERE case_id=%s AND request_id=%s", (case_id, request_id))
    if existing:
        if existing["created_by"] != g.actor or existing["body"] != body:
            raise ApiError("REQUEST_KEY_CONFLICT")
        return {"note_id": existing["note_id"]}
    note_id = run("INSERT INTO staff_note (case_id,request_id,created_by,body) VALUES (%s,%s,%s,%s)",
                  (case_id, request_id, g.actor, body))
    admin_change("직원용 기록 추가", f"기록 #{note_id}", case_id)
    return {"note_id": note_id}, 201


@management.post("/cases/<uuid:case_id>/notes/<int:note_id>")
def change_note(case_id, note_id):
    # 메모 변경 내용이 기존 값과 다를 때만 클라이언트가 본 updated_at과 DB의 최종 수정 시각을 비교한다.
    # 다른 관리자가 먼저 고친 경우에는 새 내용을 덮지 않고 새로고침할 수 있도록 오류를 반환한다.
    editable_history(case_id)
    data = payload()
    note = one("SELECT body,updated_at FROM staff_note WHERE case_id=%s AND note_id=%s", (case_id, note_id))
    delete = data.get("operation") == "delete"
    if not note or (note["body"] is None and not delete):
        raise ApiError("STAFF_NOTE_NOT_FOUND")
    body = None if delete else text_field(data.get("body"))
    if note["body"] != body:
        if data.get("updated_at") != stamp(note["updated_at"]):
            raise ApiError("STAFF_NOTE_CHANGED")
        # 삭제는 본문만 NULL로 만든다. 생성 요청 ID가 남아 지연된 재시도가 메모를 다시 만들 수 없다.
        run("UPDATE staff_note SET body=%s WHERE case_id=%s AND note_id=%s", (body, case_id, note_id))
        admin_change("직원용 기록 삭제" if delete else "직원용 기록 수정", f"기록 #{note_id}", case_id)
    return {"note_id": note_id}


@management.post("/cases/<uuid:case_id>/messages/<int:message_id>")
def edit_message(case_id, message_id):
    # 보관 중인 웹 티켓에서 본문이 남은 메시지만 정정할 수 있다. 메시지 ID와 티켓 ID를 함께 확인한다.
    editable_history(case_id, "WEB")
    message = one("SELECT body FROM chat_message WHERE case_id=%s AND message_id=%s AND body<>''", (case_id, message_id))
    if not message:
        raise ApiError("MESSAGE_NOT_FOUND")
    data = payload()
    if data.get("operation") == "delete":
        # 빈 본문은 삭제 표시다. 보관 기한 정리까지 전송 요청 ID를 남겨 재전송으로 본문이 복구되는 것을 막는다.
        run("UPDATE chat_message SET body='' WHERE case_id=%s AND message_id=%s", (case_id, message_id))
        admin_change("메시지 삭제", f"메시지 #{message_id}", case_id)
    else:
        body = text_field(data.get("body"))
        if body != message["body"]:
            run("UPDATE chat_message SET body=%s WHERE case_id=%s AND message_id=%s", (body, case_id, message_id))
            admin_change("메시지 수정", f"메시지 #{message_id}", case_id)
    return {"message_id": str(message_id)}


@management.post("/cases/<uuid:case_id>/history")
def edit_history(case_id):
    # 티켓 전체 삭제와 상담 내용만 지우는 요청을 구분한다. 내용 삭제는 티켓 자체와 배정 이력을 보존한다.
    data = payload()
    if data.get("operation") == "delete_ticket":
        return delete_ticket(case_id)
    case = editable_history(case_id)
    if data.get("operation") == "delete":
        if case["channel"] == "WEB":
            run("UPDATE chat_message SET body='' WHERE case_id=%s AND body<>''", (case_id,))
        else:
            run("UPDATE phone_response SET call_at=NULL, consent_state='UNASKED', consent_by=NULL, consent_at=NULL, "
                "inquiry=NULL, guidance=NULL, followup_required=NULL WHERE case_id=%s", (case_id,))
            run("UPDATE recording SET state='DELETE_PENDING' WHERE case_id=%s AND state IN ('PENDING','FAILED','READY')", (case_id,))
        run("UPDATE staff_note SET body=NULL WHERE case_id=%s AND body IS NOT NULL", (case_id,))
        run("UPDATE support_case SET result_note=NULL WHERE case_id=%s", (case_id,))
        admin_change("상담 기록 삭제", "웹채팅" if case["channel"] == "WEB" else "전화 응대 및 녹취", case_id)
    else:
        # 전화 응대는 요청에 들어 있는 필드만 수정하거나 비운다. 결과 메모는 종결된 티켓에서 수정한다.
        fields = {key: data[key] for key in ("call_at", "inquiry", "guidance", "followup_required") if key in data}
        if fields:
            channel(case, "PHONE")
            save_phone_response(case_id, fields, allow_clear=True)
        if "result_note" in data and case["closed_at"]:
            run("UPDATE support_case SET result_note=%s WHERE case_id=%s", (text_field(data["result_note"]), case_id))
        elif not fields:
            raise ApiError("INVALID_INPUT")
        admin_change("상담 기록 수정", "웹채팅" if case["channel"] == "WEB" else "전화 응대", case_id)
    return {"case_id": case_id}


def delete_ticket(case_id):
    # 삭제된 행도 조회할 수 있는 원본 테이블을 잠근다. 이미 처리한 삭제의 재요청에도 204를 돌려준다.
    case = one("SELECT channel,deleted_at FROM support_case WHERE case_id=%s FOR UPDATE", (case_id,))
    if not case:
        raise ApiError("CASE_NOT_FOUND")
    if case["deleted_at"] is None:
        # 본문과 입장 수단을 지우고 녹취는 정리 작업에 맡긴다. 삭제 표시된 티켓은 visible_case에서 제외된다.
        purge_case_content(case_id)
        run("DELETE FROM entry_code WHERE case_id=%s", (case_id,))
        run("DELETE FROM customer_session WHERE case_id=%s", (case_id,))
        run("UPDATE recording SET state='DELETE_PENDING' WHERE case_id=%s AND state IN ('PENDING','FAILED','READY')", (case_id,))
        # 감사 이력과 뒤늦은 접수 재시도를 구분하는 데 필요한 티켓 ID와 접수 요청 ID는 보존한다.
        run("UPDATE support_case SET deleted_at=UTC_TIMESTAMP(6),support_subject_ref=NULL,current_agent_id=NULL "
            "WHERE case_id=%s", (case_id,))
        admin_change("상담 티켓 삭제", "웹채팅" if case["channel"] == "WEB" else "전화", case_id)
    return "", 204


@management.get("/voice-recordings/<uuid:recording_id>/audio")
def play_voice(recording_id):
    # 직원 업로더가 없는 AI 녹취를 찾는다. 재생 가능 상태와 만료 여부는 공통 스트리밍 함수에서 확인한다.
    item = one("SELECT * FROM recording WHERE recording_id=%s AND uploaded_by IS NULL FOR UPDATE", (recording_id,))
    g.audit_target = ("RECORDING_PLAY", item["case_id"] if item else None, recording_id)
    if not item:
        raise ApiError("RECORDING_NOT_FOUND")
    return stream_recording(item)


@management.post("/voice-recordings/<uuid:recording_id>/summary")
def request_voice_summary(recording_id):
    # 재생 가능한 AI 녹취에 요약 작업을 예약한다. 아직 요청하지 않았거나 이전 시도가
    # 실패한 경우만 대기열에 넣어, 이미 처리 중이거나 완성된 결과를 중복 실행하지 않는다.
    item = one("SELECT r.case_id,r.state,r.expires_at,c.deleted_at FROM recording r "
               "LEFT JOIN support_case c ON c.case_id=r.case_id WHERE r.recording_id=%s AND r.uploaded_by IS NULL FOR UPDATE",
               (recording_id,))
    if not item or item["deleted_at"]:
        raise ApiError("RECORDING_NOT_FOUND")
    check_expiry(item)
    if item["state"] != "READY":
        raise ApiError("RECORDING_NOT_READY")
    previous = one("SELECT state FROM ai_call_summary WHERE recording_id=%s", (recording_id,))
    state = previous["state"] if previous else None
    if state in (None, "FAILED"):
        run("INSERT INTO ai_call_summary (recording_id) VALUES (%s) ON DUPLICATE KEY UPDATE state='QUEUED',error_message=NULL",
            (recording_id,))
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
        item = one("SELECT state,expires_at,case_id FROM recording WHERE case_id=%s AND recording_id=%s FOR UPDATE",
                   (case_id, recording_id))
    else:
        item = one("SELECT state,expires_at,case_id FROM recording WHERE uploaded_by IS NULL AND recording_id=%s FOR UPDATE",
                   (recording_id,))
    if not item:
        raise ApiError("RECORDING_NOT_FOUND")
    case_id = item["case_id"]
    data = payload()
    if data.get("operation") == "delete":
        if item["state"] not in ("DELETE_PENDING", "DESTROYED"):
            # 여기서는 상태를 바꿔 재생을 중단시킨다. 파일 제거와 파기 시각 기록은 purge가 맡는다.
            run("UPDATE recording SET state='DELETE_PENDING' WHERE recording_id=%s", (recording_id,))
            admin_change("녹취 삭제 요청", recording_id, case_id)
    else:
        check_expiry(item)
        label = data.get("label")
        if not isinstance(label, str) or len(label) > 120:
            raise ApiError("INVALID_INPUT")
        run("UPDATE recording SET label=%s WHERE recording_id=%s", (label.strip(), recording_id))
        admin_change("녹취 표시명 수정", recording_id, case_id)
    return {"recording_id": recording_id}
