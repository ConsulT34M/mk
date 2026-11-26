import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pymysql
from flask import g
from pymysql.cursors import DictCursor

# 스키마와 마이그레이션 SQL을 읽는 폴더.
SQL_DIR = Path(__file__).resolve().parent.parent / "mysql"


def db():
    # 요청이나 CLI 작업마다 MySQL 연결을 하나 열어 공유한다.
    if "connection" not in g:
        g.connection = pymysql.connect(
            host=os.environ.get("MYSQL_HOST", "db"),
            database=os.environ["MYSQL_DATABASE"],
            user=os.environ["MYSQL_USER"],
            password=os.environ["MYSQL_PASSWORD"],
            charset="utf8mb4",
            # 시각은 UTC로 맞추고, 일반 조회는 매번 최근 커밋까지 읽는다.
            cursorclass=DictCursor,
            init_command="SET time_zone='+00:00', transaction_isolation='READ-COMMITTED'",
            connect_timeout=5,
        )
    return g.connection


def close_db(_error=None):
    # 작업이 끝나면 사용한 연결을 닫는다.
    connection = g.pop("connection", None)
    if connection:
        connection.close()


def one(sql, args=()):
    # 첫 행을 딕셔너리로 반환한다. 결과가 없으면 None이다.
    with db().cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.fetchone()


def rows(sql, args=()):
    # 조회한 모든 행을 리스트로 반환한다.
    with db().cursor() as cursor:
        cursor.execute(sql, args)
        return list(cursor.fetchall())


def run(sql, args=()):
    # 변경문을 실행하고 마지막 삽입 ID를 반환한다. 커밋은 호출자가 맡는다.
    with db().cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.lastrowid


def new_id():
    # 새 UUID 문자열을 만든다.
    return str(uuid4())


def utcnow():
    # MySQL의 DATETIME과 비교할 UTC 현재 시각을 반환한다.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def stamp(value):
    # UTC 시각을 마이크로초와 Z가 붙은 문자열로 바꾼다.
    return value.isoformat(timespec="microseconds") + "Z" if value else None


def check_connection():
    # 간단한 조회로 DB 연결을 확인한다.
    one("SELECT 1 AS ok")


def audit(action, actor, case_id=None, recording_id=None, result="OK"):
    # 작업을 수행한 주체와 결과를 감사 로그에 남긴다.
    from .queries.reports import add_audit_event

    add_audit_event(action, actor, case_id, recording_id, result)


def admin_change(action, target, case_id=None):
    # 현재 관리자가 변경한 내용을 기록한다.
    from .queries.staff import add_admin_change

    add_admin_change(g.actor, action, target, case_id)
