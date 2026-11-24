import json
import os
import secrets
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

import click
from flask import Flask, g, jsonify, make_response, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from cases import agents, case_detail, cases, staff_cases
from common import (
    CHAT_COOKIE, STAFF_COOKIE, ApiError, case_row, customer_session, deadline, digest,
    payload, staff_user, text_field,
)
from db import audit, close_db, db, new_id, one, run, stamp, utcnow
from recordings import purge, recordings

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
app.config["WAV_LIMIT"] = int(os.getenv("WAV_MAX_MIB", 10)) * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = app.config["WAV_LIMIT"] + 128 * 1024
app.config["RECORDING_DIR"] = os.getenv("RECORDING_DIR", "/var/lib/tm-recordings")
app.teardown_appcontext(close_db)
app.register_blueprint(cases)
app.register_blueprint(recordings)


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
    return jsonify(code=error.code, message=error.message), error.status


@app.errorhandler(413)
def too_large(_error):
    error = ApiError("RECORDING_TOO_LARGE" if request.endpoint == "recordings.upload_recording" else "INVALID_INPUT")
    return jsonify(code=error.code, message=error.message), 413


@app.after_request
def finish(response):
    if "connection" in g:
        if response.status_code >= 400:
            db().rollback()
        target = g.get("audit_target")
        if target:
            action, case_id, recording_id = target
            result = g.get("error_code", "OK" if response.status_code < 400 else str(response.status_code))
            audit(action, g.get("actor", "anonymous"), case_id, recording_id, result)
        db().commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
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
    user = one("SELECT * FROM staff_account WHERE login_id=%s AND is_active=1", (login_id,))
    if not user or not check_password_hash(user["password_hash"], password):
        raise ApiError("STAFF_AUTH_REQUIRED")
    token = secrets.token_urlsafe(32)
    expires = deadline("STAFF_SESSION_MINUTES", 30)
    previous = request.cookies.get(STAFF_COOKIE)
    if previous:
        run("DELETE FROM staff_session WHERE token_hash=%s", (digest(previous),))
    run("INSERT INTO staff_session (token_hash,staff_id,expires_at) VALUES (%s,%s,%s)", (digest(token), user["staff_id"], expires))
    response = redirect(url_for("queue_page"), code=303) if form_login else jsonify(
        staff_id=user["staff_id"], role=user["role"], expires_at=stamp(expires),
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
    item = one("SELECT * FROM entry_code WHERE code_hash=%s FOR UPDATE", (key,))
    if not item or item["used_at"] or item["expires_at"] <= utcnow():
        raise ApiError("ENTRY_CODE_UNUSABLE")
    case = case_row(item["case_id"])
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


@app.get("/")
def home():
    if staff_user(required=False):
        return redirect(url_for("queue_page"))
    return render_template("login.html")


@app.get("/portal")
def queue_page():
    user = staff_user(required=False)
    if not user:
        return redirect(url_for("home"))
    items = staff_cases()["items"]
    team = agents()["items"] if user["role"] == "ADMIN" else []
    return render_template("queue.html", user=user, items=items, team=team)


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
    return render_template(
        "detail.html", user=user, detail=detail, case=detail["case"], actor="staff",
        recording_minutes=int(os.getenv("RECORDING_RETENTION_MINUTES", 5)),
    )


@app.get("/chat")
def chat_page():
    session = customer_session()
    if not session or request.args.get("enter") == "1":
        return render_template("entry.html")
    case = case_row(session["case_id"])
    return render_template("customer.html", case=case, actor="customer")


def demo_id(label):
    return str(uuid5(NAMESPACE_URL, "urn:tm-demo:" + label))


@app.cli.command("seed-demo")
def seed_demo():
    """Create the local demo's staff accounts, two web cases and one phone case."""
    for login_id, role in (("admin", "ADMIN"), ("agent1", "AGENT"), ("agent2", "AGENT")):
        if not one("SELECT staff_id FROM staff_account WHERE login_id=%s", (login_id,)):
            run(
                "INSERT INTO staff_account (staff_id,login_id,password_hash,role) VALUES (%s,%s,%s,%s)",
                (new_id(), login_id, generate_password_hash(os.environ[f"DEMO_{login_id.upper()}_PASSWORD"]), role),
            )
    for n in (1, 2):
        run(
            "INSERT INTO support_case (case_id, channel, support_subject_ref, intake_request_id) "
            "VALUES (%s,'WEB',%s,%s) ON DUPLICATE KEY UPDATE case_id=case_id",
            (demo_id(f"web-{n}"), demo_id(f"subject-{n}"), demo_id(f"intake-{n}")),
        )
    phone_id = demo_id("phone")
    if not one("SELECT case_id FROM support_case WHERE case_id=%s", (phone_id,)):
        run("INSERT INTO support_case (case_id,channel) VALUES (%s,'PHONE')", (phone_id,))
        run("INSERT INTO phone_response (case_id) VALUES (%s)", (phone_id,))
    db().commit()


@app.cli.command("demo-entry")
@click.option("--customer", type=click.Choice(["1", "2"]), default="1", show_default=True)
@click.option("--new", "new_case", is_flag=True, help="Open a new web case.")
def demo_entry(customer, new_case):
    """Get a chat entry code through the exchange API for a synthetic customer."""
    base = "http://127.0.0.1:8000"
    headers = {"Authorization": "Bearer " + os.environ["PARTNER_TOKEN"], "Content-Type": "application/json"}
    subject = demo_id("subject-" + customer)

    def exchange(path, data=None):
        body = None if data is None else json.dumps(data).encode()
        with urlopen(Request(base + path, data=body, headers=headers), timeout=10) as result:
            return json.load(result)

    if new_case:
        case_id = exchange("/cases", {"support_subject_ref": subject, "intake_request_id": new_id()})["case_id"]
    else:
        case_id = exchange("/cases?support_subject_ref=" + subject)["items"][0]["case_id"]
    grant = exchange(f"/cases/{case_id}/entry-codes", {"support_subject_ref": subject})
    click.echo(grant["entry_code"])


app.cli.command("purge")(purge)
