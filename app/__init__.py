import json
import os
import secrets
import time
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

import click
from dotenv import load_dotenv
from flask import Flask, g, jsonify, make_response, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from .admin import PERIODS, init_admin, management, migrate_desk, quota_rows
from .cases import agent_inbox, case_detail, cases as cases_blueprint, intake_case, recent_closed_cases, staff_notes, waiting_tickets
from .common import (
    CHAT_COOKIE, INQUIRY_COOKIE, STAFF_COOKIE, ApiError, agent, case_row, customer_session, deadline, digest,
    payload, staff_user, text_field,
)
from .db import audit, check_connection, close_db, db as get_db, new_id, stamp, utcnow
from .desk_reports import DIRECTIONS, OUTCOMES, TOPICS, call_history, case_status_breakdown, duration, notices, report_range, statistics
from .queries import cases as case_queries
from .queries import chat as chat_queries
from .queries import staff as staff_queries
from .queries import voice as voice_queries
from .recordings import purge, recordings as recordings_blueprint
from .voice import voice
from .voice_summaries import summary_worker

# 이 모듈에서 Flask 앱을 만들고 상담 티켓, 녹취, 관리자, 음성 상담 라우트를 연결한다.
# 각 Blueprint의 뷰도 아래에 등록한 앱 공통 훅을 거쳐 응답한다.
app = Flask(__name__)
load_dotenv(Path(app.root_path).parent / ".env", override=False)
# HTTPS를 종료하는 프록시가 전달한 프로토콜을 반영해 request.host_url을 구성한다.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
# WAV_LIMIT는 파일 자체의 상한이다. 전체 요청에는 multipart 필드와 헤더를 담을 여유를 둔다.
app.config["WAV_LIMIT"] = int(os.getenv("WAV_MAX_MIB", 10)) * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = app.config["WAV_LIMIT"] + 128 * 1024
app.config["RECORDING_DIR"] = os.getenv("RECORDING_DIR", "/var/lib/tm-recordings")
app.config["S3_RECORDING_BUCKET"] = os.getenv("S3_RECORDING_BUCKET", "").strip() or None
# 요청이나 CLI 명령의 앱 컨텍스트가 끝나면 그 작업에서 열었던 DB 연결을 닫는다.
app.teardown_appcontext(close_db)
# Blueprint 안의 @...get, @...post 등으로 선언한 경로가 여기서 앱의 URL 목록에 등록된다.
# 예를 들어 url_for("management.index")는 management에 등록한 index 뷰의 주소를 만든다.
app.register_blueprint(cases_blueprint)
app.register_blueprint(recordings_blueprint)
app.register_blueprint(management)
app.register_blueprint(voice)
# Jinja 템플릿의 utcstamp 필터는 DB의 일시를 UTC 문자열로 표시한다.
app.add_template_filter(stamp, "utcstamp")
# 두 함수를 Flask CLI 명령으로 등록해, 앱을 실행하는 환경에서 DB 스키마를 준비할 수 있게 한다.
app.cli.command("init-admin")(init_admin)
app.cli.command("migrate-desk")(migrate_desk)
# 화면마다 같은 코드표로 한글명을 찾고 같은 필터로 통화 길이를 표시한다.
app.jinja_env.globals.update(topic_names=TOPICS, outcome_names=OUTCOMES, direction_names=DIRECTIONS)
app.add_template_filter(duration, "duration")


@app.url_value_preprocessor
def normalize_ids(_endpoint, values):
    # <uuid:...> 경로 변수는 Flask가 UUID 객체로 변환한다. 뷰에 넘기기 전에
    # DB의 CHAR(36) 식별자 및 세션에서 읽은 ID와 같은 문자열 형식으로 맞춘다.
    for key, value in (values or {}).items():
        if isinstance(value, UUID):
            values[key] = str(value)


@app.before_request
def check_origin():
    # 뷰가 본문을 읽기 전에 일반 POST의 상한을 64 KiB로 좁힌다.
    # 별도 파일 크기 검사가 있는 직원 WAV 업로드에는 앱의 multipart 상한을 적용한다.
    if request.method != "POST":
        return
    if request.endpoint != "recordings.upload_recording":
        request.max_content_length = 64 * 1024
    # 거래소의 입장 폼은 다른 출처에서 일회용 코드를 POST한다. 코드는 enter_chat에서 검증한다.
    if request.endpoint == "enter_form":
        return
    # 그 밖의 POST에 Origin이 있으면 프로토콜·호스트·포트가 이 앱의 요청 주소와 일치해야 한다.
    origin = request.headers.get("Origin")
    if origin and origin != request.host_url.rstrip("/"):
        raise ApiError("CASE_ACCESS_DENIED")


@app.errorhandler(ApiError)
def api_failure(error):
    # finish에서 감사 기록에 남길 업무 오류 코드를 저장한다. 브라우저 폼으로 들어온
    # 로그인·입장·티켓 수락 오류는 해당 화면에 표시하고, API 호출에는 코드와 메시지를 JSON으로 보낸다.
    g.error_code = error.code
    if request.endpoint == "enter_form":
        return render_template("entry.html", error=error.message), error.status
    if request.endpoint == "login" and request.mimetype == "application/x-www-form-urlencoded":
        return render_template("login.html", error=error.message), error.status
    if request.endpoint == "cases.claim" and request.mimetype == "application/x-www-form-urlencoded":
        return render_template("denied.html", user=staff_user(required=False), error=error.message), error.status
    return jsonify(code=error.code, message=error.message), error.status


@app.errorhandler(413)
def too_large(_error):
    # Flask가 본문 크기 초과로 읽기를 중단한 경우에도 API의 공통 오류 형식으로 응답한다.
    error = ApiError("RECORDING_TOO_LARGE" if request.endpoint == "recordings.upload_recording" else "INVALID_INPUT")
    return jsonify(code=error.code, message=error.message), 413


@app.after_request
def finish(response):
    # DB를 사용한 요청만 마무리한다. 오류 응답이면 미확정 변경을 먼저 되돌리고,
    # 뷰가 g.audit_target에 지정한 작업은 실패 사유까지 감사 로그에 남겨 커밋한다.
    if "connection" in g:
        if response.status_code >= 400:
            get_db().rollback()
        target = g.get("audit_target")
        if target:
            action, case_id, recording_id = target
            result = g.get("error_code", "OK" if response.status_code < 400 else str(response.status_code))
            audit(action, g.get("actor", "anonymous"), case_id, recording_id, result)
        get_db().commit()
    # 상담 내용이 담길 수 있으므로 HTML과 JSON 모두 브라우저·중간 캐시에 저장하지 않도록 응답한다.
    response.headers["Cache-Control"] = "no-store"
    # 로그인·티켓 폼이 같은 출처로 제출될 때 브라우저가 Origin을 전달할 수 있게 한다.
    form_page = request.endpoint in {"home", "login", "queue_page", "detail_page", "inquiry_page"} and response.mimetype == "text/html"
    response.headers["Referrer-Policy"] = "same-origin" if form_page else "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    # 음성 상담을 제공하는 문의 화면에서만 브라우저의 blob 미디어와 OpenAI 연결을 허용한다.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
        + ("media-src 'self' blob:; connect-src 'self' https://api.openai.com"
           if request.endpoint == "inquiry_page" else "media-src 'self'")
    )
    return response


def session_cookie(response, name, token, expires):
    # 원본 토큰은 브라우저가 HTTPS 요청에 첨부한다. HttpOnly로 스크립트의 쿠키 읽기를 막고
    # 브라우저의 쿠키 만료 시각을 호출자가 DB에 기록한 세션 만료 시각과 맞춘다.
    response.set_cookie(name, token, expires=expires, httponly=True, secure=True, samesite="Lax", path="/")
    return response


@app.post("/staff/sessions")
def login():
    # 로그인 화면은 폼을, API 클라이언트는 JSON을 보낸다. 두 경로 모두 활성 계정의
    # 비밀번호 해시를 검증하고 같은 방식으로 세션을 발급한다.
    form_login = request.mimetype == "application/x-www-form-urlencoded"
    data = request.form if form_login else payload()
    login_id = text_field(data.get("login_id"), 80)
    password = data.get("password")
    if not isinstance(password, str) or not password or len(password) > 512:
        raise ApiError("INVALID_INPUT")
    user = staff_queries.lock_login(login_id)
    if not user or not check_password_hash(user["password_hash"], password):
        raise ApiError("STAFF_AUTH_REQUIRED")
    # 이 브라우저가 제시한 이전 세션을 교체한다. DB에는 새 토큰의 해시만 보관한다.
    token = secrets.token_urlsafe(32)
    expires = deadline("STAFF_SESSION_MINUTES", 30)
    previous = request.cookies.get(STAFF_COOKIE)
    if previous:
        staff_queries.delete_session(digest(previous))
    staff_queries.create_session(digest(token), user["staff_id"], expires)
    destination = staff_home(user)
    # 폼 제출 후에는 303으로 역할별 첫 화면을 GET하게 하고, JSON 호출에는 목적지와 만료 시각을 준다.
    response = redirect(destination, code=303) if form_login else jsonify(
        staff_id=user["staff_id"], role=user["role"], expires_at=stamp(expires), destination=destination,
    )
    return session_cookie(response, STAFF_COOKIE, token, expires)


@app.post("/staff/sessions/logout")
def logout():
    # 현재 쿠키에 해당하는 서버 세션을 삭제한 뒤, 204 응답으로 브라우저의 쿠키도 지운다.
    staff_user()
    staff_queries.delete_session(digest(request.cookies[STAFF_COOKIE]))
    response = make_response("", 204)
    response.delete_cookie(STAFF_COOKIE, secure=True, httponly=True, samesite="Lax")
    return response


def enter_chat(code, as_page=False):
    # 거래소에서 받은 일회용 코드를 특정 티켓의 채팅 세션으로 교환한다.
    if not isinstance(code, str) or not code or len(code) > 256:
        raise ApiError("ENTRY_CODE_UNUSABLE")
    key = digest(code)
    # 티켓 삭제 경로와 잠금 순서를 맞춘다. 티켓을 먼저 잠그고 이어서 코드 행을 잠가
    # 동시에 두 요청이 들어와도 한 요청만 미사용 코드를 소비할 수 있게 한다.
    case = chat_queries.lock_case_for_entry(key)
    if not case:
        raise ApiError("ENTRY_CODE_UNUSABLE")
    item = chat_queries.lock_entry_code(key)
    if not item or item["used_at"] or item["expires_at"] <= utcnow():
        raise ApiError("ENTRY_CODE_UNUSABLE")
    # 코드 사용 표시, 기존 채팅 세션 교체, 새 세션 발급은 한 요청의 트랜잭션에서 처리한다.
    chat_queries.consume_entry_code(key)
    previous = request.cookies.get(CHAT_COOKIE)
    if previous:
        chat_queries.delete_session(digest(previous))
    token = secrets.token_urlsafe(32)
    expires = deadline("CHAT_SESSION_MINUTES", 60)
    chat_queries.create_session(digest(token), item["case_id"], expires)
    # 종결된 티켓도 입장할 수 있다. JSON에는 읽기 전용 여부를 담고 폼은 채팅 화면으로 이동시킨다.
    if as_page:
        response = redirect(url_for("chat_page"), code=303)
    else:
        response = jsonify(case_id=item["case_id"], expires_at=stamp(expires), read_only=bool(case["closed_at"]))
    return session_cookie(response, CHAT_COOKIE, token, expires)


@app.post("/chat/sessions")
def chat_login():
    # JSON 클라이언트에는 티켓 ID와 만료 시각을 담은 응답에 세션 쿠키를 실어 보낸다.
    return enter_chat(payload().get("entry_code"))


@app.post("/chat/enter")
def enter_form():
    # 거래소 폼의 입장 코드는 POST 본문에서 읽고 채팅 화면으로 리디렉션한다.
    return enter_chat(request.form.get("entry_code"), as_page=True)


@app.get("/health")
def health():
    # 간단한 SQL까지 성공해야 정상으로 응답하므로 DB 연결 상태도 함께 확인된다.
    check_connection()
    return {"status": "ok"}


def staff_home(user):
    # 로그인 직후와 이미 로그인한 직원의 재방문에 같은 역할별 시작 주소를 사용한다.
    return url_for("management.index" if user["role"] == "ADMIN" else "queue_page")


@app.get("/")
def home():
    # 첫 화면은 익명 방문도 가능하다. 유효한 직원 세션이 있으면 로그인 폼을 건너뛴다.
    user = staff_user(required=False)
    if user:
        return redirect(staff_home(user))
    return render_template("login.html")


@app.get("/portal")
def queue_page():
    # 주소를 직접 열어도 세션과 역할을 확인해 로그인 화면, 관리자 화면, 상담원 데스크로 안내한다.
    user = staff_user(required=False)
    if not user:
        return redirect(url_for("home"))
    if user["role"] == "ADMIN":
        return redirect(staff_home(user))
    return agent_desk(user)


@app.get("/portal/<uuid:case_id>")
def detail_page(case_id):
    # 상세 API와 같은 담당자 검사를 재사용한다. 실패 시 상태 코드를 유지하면서
    # 브라우저에 표시할 접근 오류 페이지를 렌더링한다.
    user = staff_user(required=False)
    if not user:
        return redirect(url_for("home"))
    try:
        detail = case_detail(case_id)
    except ApiError as error:
        g.error_code = error.code
        return render_template("denied.html", user=user, error=error.message), error.status
    return agent_desk(user, detail)


def desk_lists(user, tab):
    # 전체 페이지와 부분 갱신 API가 같은 목록·건수를 사용하도록 현재 담당 티켓을 한 번 나눈다.
    owned = agent_inbox(user["staff_id"])
    active = [item for item in owned if item["status"] == "IN_PROGRESS"]
    closed = [item for item in owned if item["status"] == "CLOSED"]
    list_status = request.args.get("status", "active")
    if list_status not in ("active", "closed", "all"):
        list_status = "active"
    items = closed if tab == "closed" else active
    channel_closed = len(closed)
    if tab not in {"dashboard", "desk", "calls", "chats", "closed"}:
        tab = "dashboard"
    if tab in ("calls","chats"):
        # 채널 탭에서는 선택한 상태로 목록을 거른다. 전체 종결 건수와 채널별 종결 건수는 따로 센다.
        channel = "PHONE" if tab == "calls" else "WEB"
        channel_closed = sum(item["channel"] == channel for item in closed)
        active = [item for item in active if item["channel"] == channel]
        items = active if list_status == "active" else [
            item for item in (closed if list_status == "closed" else owned) if item["channel"] == channel
        ]
    return {
        "tab": tab, "items": items, "list_status": list_status, "tickets": waiting_tickets(),
        "recent_cases": recent_closed_cases(user["staff_id"]) if tab in ("desk", "calls", "chats") else [],
        "active_count": len(active), "closed_count": len(closed), "channel_closed_count": channel_closed,
    }


def agent_desk(user, detail=None):
    # 상세 링크로 들어온 경우와 데스크가 목록의 첫 티켓을 자동 선택한 경우를 템플릿에서 구분한다.
    explicit_case = detail is not None
    tab = request.args.get("tab")
    if detail:
        case = detail["case"]
        channel_tab = "calls" if case["channel"] == "PHONE" else "chats"
        # 채널 목록에서 '전체'나 '종결'을 보다가 연 티켓이면 기존 탭을 유지한다.
        # 그 밖의 경로로 종결 티켓을 열면 종결 탭을 선택한다.
        if case["status"] == "CLOSED" and not (tab == channel_tab and request.args.get("status") in ("all", "closed")):
            tab = "closed"
        else:
            tab = channel_tab
    if tab in ("notices", "stats", "recordings"):
        # 이 세 탭은 티켓 목록과 별도로 렌더링한다. 통화 이력은 본인 기준이고,
        # 통계의 팀 범위는 상담원이 실제 팀에 속하며 해당 범위를 선택한 경우에 적용한다.
        data = {}
        if tab == "notices":
            data["notices"] = notices()
        else:
            dates = report_range()
            data["dates"] = dates
            if tab == "recordings":
                data["calls"] = call_history(user["staff_id"], dates)
            else:
                team = staff_queries.team_name(user["team_id"]) if user["team_id"] else None
                own_team = bool(team and request.args.get("scope") == "team")
                data.update(team=team, scope="team" if own_team else "mine",
                            report=statistics(dates, staff_id=user["staff_id"], own_team=own_team))
        return render_template("agent.html", user=user, tab=tab, **data)
    data = desk_lists(user, tab)
    if not detail and data["tab"] != "dashboard" and data["items"]:
        # 목록을 읽은 뒤 첫 티켓이 재배정되거나 삭제될 수 있다. 이때는 목록을 다시 읽게 한다.
        try:
            detail = case_detail(data["items"][0]["case_id"])
        except ApiError as error:
            if error.code not in ("CASE_ACCESS_DENIED", "CASE_NOT_FOUND"):
                raise
            return redirect(url_for("queue_page", tab=data["tab"]))
    if not detail:
        g.audit_target = ("CASE_LIST", None, None)
    case = detail["case"] if detail else None
    data["notices"] = notices(latest=True)
    if data["tab"] == "dashboard":
        data.update(quota=quota_rows(user["staff_id"])[0], periods=PERIODS, case_summary=case_status_breakdown())
    return render_template(
        "agent.html", user=user, detail=detail, case=case, selected_id=case["case_id"] if case else None,
        explicit_case=explicit_case, recording_minutes=int(os.getenv("RECORDING_RETENTION_MINUTES", 5)), **data,
    )


@app.get("/portal/updates")
def desk_updates():
    # 데스크의 주기적 갱신 요청에 건수와 서버에서 렌더링한 HTML 조각을 JSON으로 응답한다.
    # 선택된 티켓 ID는 목록 강조와 담당자 범위의 메모 조회에 사용한다.
    user = agent()
    data = desk_lists(user, request.args.get("tab"))
    g.audit_target = ("CASE_LIST", None, None)
    template = "_tickets.html" if data["tab"] == "dashboard" else "_inbox.html"
    selected_id = request.args.get("case_id")
    result = {
        "waiting": len(data["tickets"]), "active": data["active_count"], "closed": data["closed_count"],
        "listed": len(data["items"]), "channel_closed": data["channel_closed_count"],
        "notice_html": render_template("_notices.html", notices=notices(latest=True)),
        "html": render_template(template, selected_id=selected_id, **data),
    }
    if data["tab"] == "dashboard":
        result["quota_html"] = render_template("_quota_rows.html", quota=quota_rows(user["staff_id"])[0], periods=PERIODS)
        result["chart_html"] = render_template("_case_chart.html", case_summary=case_status_breakdown())
    elif data["tab"] != "closed":
        result["history_html"] = render_template("_agent_history.html", recent_cases=data["recent_cases"])
    if data["tab"] != "dashboard" and selected_id:
        result["notes_html"] = render_template("_agent_notes.html", notes=staff_notes(selected_id, agent_id=user["staff_id"]))
    return result


@app.get("/chat")
def chat_page():
    # 교환한 코드로 발급받은 채팅 쿠키에서 티켓을 찾는다. enter=1이면 코드 입력 화면을 다시 연다.
    session = customer_session()
    if not session or not session["case_id"] or request.args.get("enter") == "1":
        return render_template("entry.html")
    case = case_row(session["case_id"])
    return render_template("customer.html", case=case, actor="customer")


@app.get("/inquiry")
def inquiry_page():
    # 공개 문의 페이지는 방문 세션만으로 열 수 있다. 이미 연결된 상담이 있으면 상태를 복원한다.
    visitor = customer_session(INQUIRY_COOKIE)
    case = None
    if visitor and visitor["case_id"]:
        case = case_queries.inquiry_status(visitor["case_id"])
    policy_on = bool(voice_queries.policy()["enabled"])
    # 운영자가 정한 음성 상담 상태와 실제 시작 가능 여부(서버 키 설정 포함)를 화면에 각각 전달한다.
    response = make_response(render_template("inquiry.html", case=case, opened=request.args.get("chat") == "open", policy_on=policy_on,
                                             voice_enabled=policy_on and bool(os.environ.get("OPENAI_API_KEY", "").strip()),
                                             recording_minutes=int(os.getenv("RECORDING_RETENTION_MINUTES", 5))))
    if not visitor:
        # 첫 방문에서는 티켓 없이 방문 세션만 발급한다. 채팅 접수는 open_inquiry에서 처리한다.
        token = secrets.token_urlsafe(32)
        expires = deadline("CHAT_SESSION_MINUTES", 60)
        chat_queries.create_visitor_session(digest(token), expires)
        session_cookie(response, INQUIRY_COOKIE, token, expires)
    return response


@app.post("/inquiry/open")
def open_inquiry():
    # 방문 세션을 먼저 잠가 같은 브라우저의 두 탭에서 동시에 접수해도 한 티켓에 연결되게 한다.
    visitor = customer_session(INQUIRY_COOKIE, lock=True)
    if not visitor:
        return redirect(url_for("inquiry_page", reconnect=1), code=303)
    previous = visitor["case_id"]
    # 첫 접수이거나 폼의 직전 티켓 ID가 현재 연결된 종결 티켓과 일치할 때만 새 문의를 만든다.
    # 이미 처리한 폼이 다시 제출되면 기존에 연결된 상담으로 돌아간다.
    new_inquiry = previous and request.form.get("closed_case_id") == previous and case_queries.closed_case(previous)
    response = redirect(url_for("inquiry_page", chat="open"), code=303)
    if previous is None or new_inquiry:
        case_id, _ = intake_case(new_id(), new_id(), "WEB", request.form.get("inquiry_type") or None)
        token = request.cookies[INQUIRY_COOKIE]
        expires = deadline("CHAT_SESSION_MINUTES", 60)
        chat_queries.link_session(digest(token), case_id, expires)
        session_cookie(response, INQUIRY_COOKIE, token, expires)
    return response


def demo_id(label):
    # 같은 이름에서 항상 같은 UUID를 만들어 데모 초기화와 CLI 조회가 같은 고객·티켓을 가리키게 한다.
    return str(uuid5(NAMESPACE_URL, "urn:tm-demo:" + label))


@app.cli.command("seed-demo")
def seed_demo():
    """Create the local demo's staff accounts, two web cases and one phone case."""
    # 계정 테이블이 비었을 때만 데모 직원을 만든다. 티켓은 고정 ID를 써서 재실행 때 중복 접수를 막는다.
    if not staff_queries.any_account():
        for login_id, role in (("admin", "ADMIN"), ("test", "AGENT"), ("agent1", "AGENT"), ("agent2", "AGENT")):
            password = "test" if login_id == "test" else os.environ[f"DEMO_{login_id.upper()}_PASSWORD"]
            staff_queries.seed_account(new_id(), login_id, generate_password_hash(password), role)
    for n in (1, 2):
        case_queries.seed_web_case(demo_id(f"web-{n}"), demo_id(f"subject-{n}"), demo_id(f"intake-{n}"))
    phone_id = demo_id("phone")
    if not case_queries.existing_case(phone_id):
        case_queries.seed_phone_case(phone_id, demo_id("subject-1"), demo_id("intake-phone"))
        voice_queries.create_phone_response(phone_id)
    get_db().commit()


@app.cli.command("demo-entry")
@click.option("--customer", type=click.Choice(["1", "2"]), default="1", show_default=True)
@click.option("--channel", "kind", type=click.Choice(["WEB", "PHONE"]), default="WEB", show_default=True)
@click.option("--new", "new_case", is_flag=True, help="Open a new ticket through the exchange API.")
def demo_entry(customer, kind, new_case):
    """Print a web chat entry code or a phone ticket ID for a synthetic customer."""
    # 데모 CLI도 로컬 거래소 연동 API를 호출한다. --new는 새 접수를, 기본 동작은 최신 티켓을 선택한다.
    base = "http://127.0.0.1:8000"
    headers = {"Authorization": "Bearer " + os.environ["PARTNER_TOKEN"], "Content-Type": "application/json"}
    subject = demo_id("subject-" + customer)

    def exchange(path, data=None):
        # 본문이 없으면 GET, JSON 본문이 있으면 POST로 보내 거래소용 응답을 읽는다.
        body = None if data is None else json.dumps(data).encode()
        with urlopen(Request(base + path, data=body, headers=headers), timeout=10) as result:
            return json.load(result)

    if new_case:
        case_id = exchange("/cases", {"support_subject_ref": subject, "intake_request_id": new_id(), "channel": kind})["case_id"]
    else:
        items = exchange(f"/cases?support_subject_ref={subject}&channel={kind}")["items"]
        if not items:
            raise click.ClickException("이 채널에 접수된 티켓이 없습니다. --new로 접수해 주세요.")
        case_id = items[0]["case_id"]
    if kind == "PHONE":
        # 전화 상담은 티켓 ID를 출력한다. 웹 채팅만 브라우저 입장에 쓸 일회용 코드가 필요하다.
        click.echo(case_id)
        return
    grant = exchange(f"/cases/{case_id}/entry-codes", {"support_subject_ref": subject})
    click.echo(grant["entry_code"])


app.cli.command("purge")(purge)


@app.cli.command("maintain")
def maintain():
    """Run retention cleanup and AI call summaries independently."""
    # Flask CLI에서 상시 실행하는 유지보수 명령의 진입점이다.
    run_maintenance(app)


def run_maintenance(application):
    # 요약 작업은 별도 스레드에서 진행한다. 보관 기한 정리는 매 회 새 앱 컨텍스트를 열어
    # DB 연결을 정리하고, 한 번 실패해도 30초 뒤 다음 회차에서 다시 시도한다.
    Thread(target=summary_worker, args=(application,), daemon=True).start()
    while True:
        try:
            with application.app_context():
                purge()
        except Exception:
            application.logger.exception("Retention cleanup will retry")
        time.sleep(30)
