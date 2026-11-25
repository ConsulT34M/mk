import os
from datetime import datetime, timezone
from uuid import uuid4

import pymysql
from flask import g
from pymysql.cursors import DictCursor


def db():
    # 첫 SQL 실행 시 연결을 열어 Flask의 g에 저장한다. 같은 요청 또는 CLI 앱 컨텍스트에서
    # 호출한 함수들은 이 연결을 공유하므로 여러 SQL 문을 한 트랜잭션으로 묶을 수 있다.
    if "connection" not in g:
        g.connection = pymysql.connect(
            host=os.environ.get("MYSQL_HOST", "db"),
            database=os.environ["MYSQL_DATABASE"],
            user=os.environ["MYSQL_USER"],
            password=os.environ["MYSQL_PASSWORD"],
            charset="utf8mb4",
            # 열 이름으로 결과를 읽고, 시각은 UTC로 맞춘다. READ COMMITTED에서는
            # 같은 트랜잭션 안에서도 다음 일반 조회가 그사이 다른 연결에서 커밋된 값을 읽을 수 있다.
            cursorclass=DictCursor,
            init_command="SET time_zone='+00:00', transaction_isolation='READ-COMMITTED'",
            connect_timeout=5,
        )
    return g.connection


def close_db(_error=None):
    # teardown_appcontext가 예외 유무와 관계없이 호출한다. 연결을 사용한 작업만 닫을 대상이 있다.
    connection = g.pop("connection", None)
    if connection:
        connection.close()


def one(sql, args=()):
    # 인자를 SQL에 바인딩하고 첫 행을 딕셔너리로 돌려준다. 일치하는 행이 없으면 None이다.
    with db().cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.fetchone()


def rows(sql, args=()):
    # 커서를 닫기 전에 결과 전체를 리스트로 읽어 둔다. 빈 결과는 호출자가 순회할 수 있는 빈 목록이다.
    with db().cursor() as cursor:
        cursor.execute(sql, args)
        return list(cursor.fetchall())


def run(sql, args=()):
    # 변경문을 실행하고 커서의 lastrowid를 돌려준다. 자동 증가 ID가 필요한 INSERT에서 이 값을 쓴다.
    # 커밋 시점은 요청의 finish 훅이나 명시적으로 트랜잭션을 나누는 호출자가 정한다.
    with db().cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.lastrowid


def new_id():
    # DB 식별자 열과 URL 양쪽에서 쓸 무작위 UUID 문자열을 만든다.
    return str(uuid4())


def utcnow():
    # MySQL DATETIME을 읽으면 시간대 정보가 없는 datetime을 받는다. 비교하는 값도 UTC로 구한 뒤
    # 시간대 표지만 제거해 DB의 UTC 일시와 직접 비교할 수 있게 한다.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def stamp(value):
    # API와 템플릿에는 마이크로초까지 표시하고 Z로 UTC임을 명시한다. 빈 DB 값은 None으로 전달한다.
    return value.isoformat(timespec="microseconds") + "Z" if value else None


def audit(action, actor, case_id=None, recording_id=None, result="OK"):
    # 조회·재생·정리 같은 이벤트의 주체와 결과를 기록한다. 대상이 있는 경우에만 연관 ID를 남긴다.
    run(
        "INSERT INTO audit_event (action, actor, case_id, recording_id, result) "
        "VALUES (%s, %s, %s, %s, %s)",
        (action, actor, case_id, recording_id, result),
    )


def admin_change(action, target, case_id=None):
    # 관리자 화면의 변경 이력에 업무명과 대상 설명을 남긴다. 작업자 ID는 인증 단계에서 g.actor에 담긴다.
    run(
        "INSERT INTO admin_change (actor_id, action, target, case_id) VALUES (%s,%s,%s,%s)",
        (g.actor, action, target, case_id),
    )
