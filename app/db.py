import os
from datetime import datetime, timezone
from uuid import uuid4

import pymysql
from flask import g
from pymysql.cursors import DictCursor


def db():
    if "connection" not in g:
        g.connection = pymysql.connect(
            host=os.environ.get("MYSQL_HOST", "db"),
            database=os.environ["MYSQL_DATABASE"],
            user=os.environ["MYSQL_USER"],
            password=os.environ["MYSQL_PASSWORD"],
            charset="utf8mb4",
            cursorclass=DictCursor,
            init_command="SET time_zone='+00:00', transaction_isolation='READ-COMMITTED'",
            connect_timeout=5,
        )
    return g.connection


def close_db(_error=None):
    connection = g.pop("connection", None)
    if connection:
        connection.close()


def one(sql, args=()):
    with db().cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.fetchone()


def rows(sql, args=()):
    with db().cursor() as cursor:
        cursor.execute(sql, args)
        return list(cursor.fetchall())


def run(sql, args=()):
    with db().cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.lastrowid


def new_id():
    return str(uuid4())


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def stamp(value):
    return value.isoformat(timespec="microseconds") + "Z" if value else None


def audit(action, actor, case_id=None, recording_id=None, result="OK"):
    run(
        "INSERT INTO audit_event (action, actor, case_id, recording_id, result) "
        "VALUES (%s, %s, %s, %s, %s)",
        (action, actor, case_id, recording_id, result),
    )


def admin_change(action, target, case_id=None):
    run(
        "INSERT INTO admin_change (actor_id, action, target, case_id) VALUES (%s,%s,%s,%s)",
        (g.actor, action, target, case_id),
    )
