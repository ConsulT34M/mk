"""직원 게시판 글 조회와 변경."""

from ..db import one, rows, run


def count_posts():
    return one("SELECT COUNT(*) AS total FROM staff_board_post WHERE title IS NOT NULL")["total"]


def list_posts(limit, offset):
    return rows(
        "SELECT p.post_id,p.title,p.created_at,COALESCE(NULLIF(a.display_name,''),a.login_id) AS author "
        "FROM staff_board_post p JOIN staff_account a ON a.staff_id=p.author_id "
        "WHERE p.title IS NOT NULL ORDER BY p.created_at DESC,p.post_id DESC LIMIT %s OFFSET %s",
        (limit, offset),
    )


def get_post(post_id, lock=False):
    return one(
        "SELECT p.*,a.role AS author_role,COALESCE(NULLIF(a.display_name,''),a.login_id) AS author FROM staff_board_post p "
        "JOIN staff_account a ON a.staff_id=p.author_id WHERE p.post_id=%s" + (" FOR UPDATE" if lock else ""),
        (post_id,),
    )


def create_post(post_id, author_id, title, body):
    run("INSERT INTO staff_board_post (post_id,author_id,title,body) VALUES (%s,%s,%s,%s)",
        (post_id, author_id, title, body))


def set_content(post_id, title, body):
    run("UPDATE staff_board_post SET title=%s,body=%s,revision=revision+1 WHERE post_id=%s", (title, body, post_id))
