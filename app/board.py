"""상담원과 관리자가 함께 사용하는 직원 게시판."""

from flask import Blueprint, redirect, render_template, request, url_for
from pymysql.err import IntegrityError

from .common import ApiError, payload, staff_user, text_field, uuid_field
from .db import new_id
from .queries import board as posts
from .queries.staff import notices

board = Blueprint("board", __name__, url_prefix="/staff/board")
PAGE_SIZE = 20


def can_delete_post(user, post):
    return user["staff_id"] == post["author_id"] or (user["role"] == "ADMIN" and post["author_role"] == "AGENT")


def board_page(user):
    page = max(1, request.args.get("page", 1, type=int))
    writing = request.args.get("write") == "1"
    post = None
    if "post" in request.args:
        post = posts.get_post(uuid_field(request.args["post"]))
        if not post or post["title"] is None:
            return render_template("denied.html", user=user, error="게시글을 찾을 수 없습니다."), 404
        if writing and post["author_id"] != user["staff_id"]:
            return render_template("denied.html", user=user, error="작성자만 게시글을 수정할 수 있습니다."), 403
    context = {"can_delete": bool(post and can_delete_post(user, post))}
    if writing:
        context["post_id"] = post["post_id"] if post else new_id()
    elif not post:
        total = posts.count_posts()
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        context.update(items=posts.list_posts(PAGE_SIZE, (page - 1) * PAGE_SIZE), pages=pages, notices=notices())
        if user["role"] == "ADMIN":
            context["notice_id"] = new_id()
    return render_template("board.html", tab="board", user=user, post=post, writing=writing, page=page, **context)


def saved(user, post_id=None, status=200):
    endpoint = "management.index" if user["role"] == "ADMIN" else "queue_page"
    location = url_for(endpoint, tab="board", post=post_id, page=request.args.get("page", type=int))
    if request.is_json:
        return {"post_id": post_id, "location": location}, status
    return redirect(location, code=303)


@board.post("/posts")
def create():
    user = staff_user()
    data = payload() if request.is_json else request.form
    post_id = uuid_field(data.get("post_id"))
    title, body = text_field(data.get("title"), 100), text_field(data.get("body"))
    try:
        posts.create_post(post_id, user["staff_id"], title, body)
    except IntegrityError as error:
        if error.args[0] != 1062:
            raise
        old = posts.get_post(post_id)
        if not old or (old["author_id"], old["title"], old["body"]) != (user["staff_id"], title, body):
            raise ApiError("REQUEST_KEY_CONFLICT") from None
        return saved(user, post_id)
    return saved(user, post_id, 201)


@board.post("/posts/<uuid:post_id>")
def change(post_id):
    user = staff_user()
    data = payload() if request.is_json else request.form
    post = posts.get_post(post_id, lock=True)
    if not post:
        raise ApiError("BOARD_POST_NOT_FOUND")
    operation = data.get("operation")
    allowed = can_delete_post(user, post) if operation == "delete" else post["author_id"] == user["staff_id"]
    if not allowed:
        raise ApiError("CASE_ACCESS_DENIED")
    if operation == "delete":
        title, body = None, None
    elif operation == "edit":
        if post["title"] is None:
            raise ApiError("BOARD_POST_NOT_FOUND")
        title, body = text_field(data.get("title"), 100), text_field(data.get("body"))
    else:
        raise ApiError("INVALID_INPUT")
    if (post["title"], post["body"]) != (title, body):
        if str(data.get("revision")) != str(post["revision"]):
            raise ApiError("BOARD_POST_CHANGED")
        posts.set_content(post_id, title, body)
    return saved(user, post_id if operation == "edit" else None)
