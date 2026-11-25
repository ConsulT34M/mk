import json
import os
import secrets
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

import click
from flask import Flask, g, jsonify, make_response, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from .admin import PERIODS, init_admin, management, migrate_desk, quota_rows
from .cases import agent_inbox, case_detail, cases as cases_blueprint, intake_case, recent_closed_cases, staff_notes, waiting_tickets
from .common import (
    CHAT_COOKIE, INQUIRY_COOKIE, STAFF_COOKIE, ApiError, agent, case_row, customer_session, deadline, digest,
    payload, staff_user, text_field,
)
from .db import audit, close_db, db as get_db, new_id, one, run, stamp, utcnow
from .desk_reports import DIRECTIONS, OUTCOMES, TOPICS, call_history, case_status_breakdown, duration, notices, report_range, statistics
from .recordings import purge, recordings as recordings_blueprint
from .voice import voice

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
app.config["WAV_LIMIT"] = int(os.getenv("WAV_MAX_MIB", 10)) * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = app.config["WAV_LIMIT"] + 128 * 1024
app.config["RECORDING_DIR"] = os.getenv("RECORDING_DIR", "/var/lib/tm-recordings")
app.teardown_appcontext(close_db)
app.register_blueprint(cases_blueprint)
app.register_blueprint(recordings_blueprint)
app.register_blueprint(management)
app.register_blueprint(voice)
app.add_template_filter(stamp, "utcstamp")
app.cli.command("init-admin")(init_admin)
app.cli.command("migrate-desk")(migrate_desk)
app.jinja_env.globals.update(topic_names=TOPICS, outcome_names=OUTCOMES, direction_names=DIRECTIONS)
app.add_template_filter(duration, "duration")


@app.url_value_preprocessor
def normalize_ids(_endpoint, values):
    for key, value in (values or {}).items():
        if isinstance(value, UUID):
            values[key] = str(value)


@app.before_request
def check_origin():
    if request.method != "POST":
        return
    if request.endpoint != "recordings.upload_recording":
        request.max_content_length = 64 * 1024
    # A top-level POST from the exchange is the hand-off for a single-use code.
    if request.endpoint == "enter_form":
        return
    origin = request.headers.get("Origin")
    if origin and origin != request.host_url.rstrip("/"):
        raise ApiError("CASE_ACCESS_DENIED")


@app.errorhandler(ApiError)
def api_failure(error):
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
    error = ApiError("RECORDING_TOO_LARGE" if request.endpoint == "recordings.upload_recording" else "INVALID_INPUT")
    return jsonify(code=error.code, message=error.message), 413


@app.after_request
def finish(response):
    if "connection" in g:
        if response.status_code >= 400:
            get_db().rollback()
        target = g.get("audit_target")
        if target:
            action, case_id, recording_id = target
            result = g.get("error_code", "OK" if response.status_code < 400 else str(response.status_code))
            audit(action, g.get("actor", "anonymous"), case_id, recording_id, result)
        get_db().commit()
    response.headers["Cache-Control"] = "no-store"
    # Native login and ticket forms need their same-origin Origin header.
    form_page = request.endpoint in {"home", "login", "queue_page", "detail_page", "inquiry_page"} and response.mimetype == "text/html"
    response.headers["Referrer-Policy"] = "same-origin" if form_page else "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
        + ("media-src 'self' blob:; connect-src 'self' https://api.openai.com"
           if request.endpoint == "inquiry_page" else "media-src 'self'")
    )
    return response


def session_cookie(response, name, token, expires):
    response.set_cookie(name, token, expires=expires, httponly=True, secure=True, samesite="Lax", path="/")
    return response


@app.post("/staff/sessions")
def login():
    form_login = request.mimetype == "application/x-www-form-urlencoded"
    data = request.form if form_login else payload()
    login_id = text_field(data.get("login_id"), 80)
    password = data.get("password")
    if not isinstance(password, str) or not password or len(password) > 512:
        raise ApiError("INVALID_INPUT")
    user = one("SELECT * FROM staff_account WHERE login_id=%s AND is_active=1 FOR UPDATE", (login_id,))
    if not user or not check_password_hash(user["password_hash"], password):
        raise ApiError("STAFF_AUTH_REQUIRED")
    token = secrets.token_urlsafe(32)
    expires = deadline("STAFF_SESSION_MINUTES", 30)
    previous = request.cookies.get(STAFF_COOKIE)
    if previous:
        run("DELETE FROM staff_session WHERE token_hash=%s", (digest(previous),))
    run("INSERT INTO staff_session (token_hash,staff_id,expires_at) VALUES (%s,%s,%s)", (digest(token), user["staff_id"], expires))
    destination = staff_home(user)
    response = redirect(destination, code=303) if form_login else jsonify(
        staff_id=user["staff_id"], role=user["role"], expires_at=stamp(expires), destination=destination,
    )
    return session_cookie(response, STAFF_COOKIE, token, expires)


@app.post("/staff/sessions/logout")
def logout():
    staff_user()
    run("DELETE FROM staff_session WHERE token_hash=%s", (digest(request.cookies[STAFF_COOKIE]),))
    response = make_response("", 204)
    response.delete_cookie(STAFF_COOKIE, secure=True, httponly=True, samesite="Lax")
    return response


def enter_chat(code, as_page=False):
    if not isinstance(code, str) or not code or len(code) > 256:
        raise ApiError("ENTRY_CODE_UNUSABLE")
    key = digest(code)
    # Lock the ticket before its entry code, in the same order as ticket deletion.
    case = one("SELECT case_id,closed_at FROM visible_case WHERE case_id="
               "(SELECT case_id FROM entry_code WHERE code_hash=%s) FOR UPDATE", (key,))
    if not case:
        raise ApiError("ENTRY_CODE_UNUSABLE")
    item = one("SELECT * FROM entry_code WHERE code_hash=%s FOR UPDATE", (key,))
    if not item or item["used_at"] or item["expires_at"] <= utcnow():
        raise ApiError("ENTRY_CODE_UNUSABLE")
    run("UPDATE entry_code SET used_at=UTC_TIMESTAMP(6) WHERE code_hash=%s", (key,))
    previous = request.cookies.get(CHAT_COOKIE)
    if previous:
        run("DELETE FROM customer_session WHERE token_hash=%s", (digest(previous),))
    token = secrets.token_urlsafe(32)
    expires = deadline("CHAT_SESSION_MINUTES", 60)
    run("INSERT INTO customer_session (token_hash,case_id,expires_at) VALUES (%s,%s,%s)", (digest(token), item["case_id"], expires))
    if as_page:
        response = redirect(url_for("chat_page"), code=303)
    else:
        response = jsonify(case_id=item["case_id"], expires_at=stamp(expires), read_only=bool(case["closed_at"]))
    return session_cookie(response, CHAT_COOKIE, token, expires)


@app.post("/chat/sessions")
def chat_login():
    return enter_chat(payload().get("entry_code"))


@app.post("/chat/enter")
def enter_form():
    return enter_chat(request.form.get("entry_code"), as_page=True)


@app.get("/health")
def health():
    one("SELECT 1 AS ok")
    return {"status": "ok"}


def staff_home(user):
    return url_for("management.index" if user["role"] == "ADMIN" else "queue_page")


@app.get("/")
def home():
    user = staff_user(required=False)
    if user:
        return redirect(staff_home(user))
    return render_template("login.html")


@app.get("/portal")
def queue_page():
    user = staff_user(required=False)
    if not user:
        return redirect(url_for("home"))
    if user["role"] == "ADMIN":
        return redirect(staff_home(user))
    return agent_desk(user)


@app.get("/portal/<uuid:case_id>")
def detail_page(case_id):
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
    explicit_case = detail is not None
    tab = request.args.get("tab")
    if detail:
        case = detail["case"]
        channel_tab = "calls" if case["channel"] == "PHONE" else "chats"
        if case["status"] == "CLOSED" and not (tab == channel_tab and request.args.get("status") in ("all", "closed")):
            tab = "closed"
        else:
            tab = channel_tab
    if tab in ("notices", "stats", "recordings"):
        data = {}
        if tab == "notices":
            data["notices"] = notices()
        else:
            dates = report_range()
            data["dates"] = dates
            if tab == "recordings":
                data["calls"] = call_history(user["staff_id"], dates)
            else:
                team = one("SELECT name FROM staff_team WHERE team_id=%s", (user["team_id"],)) if user["team_id"] else None
                own_team = bool(team and request.args.get("scope") == "team")
                data.update(team=team, scope="team" if own_team else "mine",
                            report=statistics(dates, staff_id=user["staff_id"], own_team=own_team))
        return render_template("agent.html", user=user, tab=tab, **data)
    data = desk_lists(user, tab)
    if not detail and data["tab"] != "dashboard" and data["items"]:
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
    session = customer_session()
    if not session or not session["case_id"] or request.args.get("enter") == "1":
        return render_template("entry.html")
    case = case_row(session["case_id"])
    return render_template("customer.html", case=case, actor="customer")


@app.get("/inquiry")
def inquiry_page():
    visitor = customer_session(INQUIRY_COOKIE)
    case = None
    if visitor and visitor["case_id"]:
        case = one("SELECT case_id,current_agent_id,closed_at FROM visible_case WHERE case_id=%s", (visitor["case_id"],))
    policy_on = bool(one("SELECT enabled FROM voice_policy WHERE policy_id=1")["enabled"])
    response = make_response(render_template("inquiry.html", case=case, opened=request.args.get("chat") == "open", policy_on=policy_on,
                                             voice_enabled=policy_on and bool(os.environ.get("OPENAI_API_KEY", "").strip()),
                                             recording_minutes=int(os.getenv("RECORDING_RETENTION_MINUTES", 5))))
    if not visitor:
        token = secrets.token_urlsafe(32)
        expires = deadline("CHAT_SESSION_MINUTES", 60)
        run("INSERT INTO customer_session (token_hash,expires_at) VALUES (%s,%s)", (digest(token), expires))
        session_cookie(response, INQUIRY_COOKIE, token, expires)
    return response


@app.post("/inquiry/open")
def open_inquiry():
    # Lock the browser session so two tabs opening the chat can only create one ticket.
    visitor = customer_session(INQUIRY_COOKIE, lock=True)
    if not visitor:
        return redirect(url_for("inquiry_page", reconnect=1), code=303)
    previous = visitor["case_id"]
    new_inquiry = previous and request.form.get("closed_case_id") == previous and one(
        "SELECT case_id FROM visible_case WHERE case_id=%s AND closed_at IS NOT NULL", (previous,))
    response = redirect(url_for("inquiry_page", chat="open"), code=303)
    if previous is None or new_inquiry:
        case_id, _ = intake_case(new_id(), new_id(), "WEB", request.form.get("inquiry_type") or None)
        token = request.cookies[INQUIRY_COOKIE]
        expires = deadline("CHAT_SESSION_MINUTES", 60)
        run("UPDATE customer_session SET case_id=%s,expires_at=%s WHERE token_hash=%s", (case_id, expires, digest(token)))
        session_cookie(response, INQUIRY_COOKIE, token, expires)
    return response


def demo_id(label):
    return str(uuid5(NAMESPACE_URL, "urn:tm-demo:" + label))


@app.cli.command("seed-demo")
def seed_demo():
    """Create the local demo's staff accounts, two web cases and one phone case."""
    if not one("SELECT staff_id FROM staff_account LIMIT 1"):
        for login_id, role in (("admin", "ADMIN"), ("test", "AGENT"), ("agent1", "AGENT"), ("agent2", "AGENT")):
            password = "test" if login_id == "test" else os.environ[f"DEMO_{login_id.upper()}_PASSWORD"]
            run(
                "INSERT INTO staff_account (staff_id,login_id,password_hash,role) VALUES (%s,%s,%s,%s)",
                (new_id(), login_id, generate_password_hash(password), role),
            )
    for n in (1, 2):
        run(
            "INSERT INTO support_case (case_id, channel, support_subject_ref, intake_request_id) "
            "VALUES (%s,'WEB',%s,%s) ON DUPLICATE KEY UPDATE case_id=case_id",
            (demo_id(f"web-{n}"), demo_id(f"subject-{n}"), demo_id(f"intake-{n}")),
        )
    phone_id = demo_id("phone")
    if not one("SELECT case_id FROM support_case WHERE case_id=%s", (phone_id,)):
        run(
            "INSERT INTO support_case (case_id,channel,support_subject_ref,intake_request_id) VALUES (%s,'PHONE',%s,%s)",
            (phone_id, demo_id("subject-1"), demo_id("intake-phone")),
        )
        run("INSERT INTO phone_response (case_id) VALUES (%s)", (phone_id,))
    get_db().commit()


@app.cli.command("demo-entry")
@click.option("--customer", type=click.Choice(["1", "2"]), default="1", show_default=True)
@click.option("--channel", "kind", type=click.Choice(["WEB", "PHONE"]), default="WEB", show_default=True)
@click.option("--new", "new_case", is_flag=True, help="Open a new ticket through the exchange API.")
def demo_entry(customer, kind, new_case):
    """Print a web chat entry code or a phone ticket ID for a synthetic customer."""
    base = "http://127.0.0.1:8000"
    headers = {"Authorization": "Bearer " + os.environ["PARTNER_TOKEN"], "Content-Type": "application/json"}
    subject = demo_id("subject-" + customer)

    def exchange(path, data=None):
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
        click.echo(case_id)
        return
    grant = exchange(f"/cases/{case_id}/entry-codes", {"support_subject_ref": subject})
    click.echo(grant["entry_code"])


app.cli.command("purge")(purge)
