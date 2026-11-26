"""기존 설치와 신규 설치에 같은 관리자·상담 데스크 스키마를 적용한다."""


def init_admin():
    """Upgrade existing demo volumes in place and initialize fresh installations."""
    from app.db import SQL_DIR, one, run

    # 이미 데이터가 있는 DB에도 적용하는 Flask CLI 명령이다. visible_case 뷰에서 참조할
    # 삭제 표시 열을 먼저 만든 뒤 관리자용 뷰와 테이블을 준비한다.
    if not one("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() "
               "AND table_name='support_case' AND column_name='deleted_at'"):
        run("ALTER TABLE support_case ADD COLUMN deleted_at DATETIME(6)")
    sql = (SQL_DIR / "admin_schema.sql").read_text(encoding="utf-8")
    for statement in sql.split(";"):
        if statement.strip():
            run(statement)
    # 기존 목표는 기본값을 도입하는 이번 업그레이드에서만 일괄 변경한다.
    if one("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='agent_quota' "
           "AND column_name='daily' AND column_default IS NULL"):
        run("UPDATE agent_quota q JOIN staff_account a USING (staff_id) SET q.daily=10,q.weekly=50,q.monthly=200 "
            "WHERE a.role='AGENT'")
        run("ALTER TABLE agent_quota ALTER COLUMN daily SET DEFAULT 10, ALTER COLUMN weekly SET DEFAULT 50, "
            "ALTER COLUMN monthly SET DEFAULT 200")
    run("INSERT IGNORE INTO agent_quota (staff_id) SELECT a.staff_id FROM staff_account a "
        "LEFT JOIN agent_quota q USING (staff_id) WHERE a.role='AGENT' AND q.staff_id IS NULL")
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
    if not one("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() "
               "AND table_name='recording' AND column_name='storage_bucket'"):
        run("ALTER TABLE recording ADD COLUMN storage_bucket VARCHAR(63)")
    if not one("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() "
               "AND table_name='recording' AND column_name='call_started_at'"):
        run("ALTER TABLE recording ADD COLUMN call_started_at DATETIME(6)")
    if one("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='recording' "
           "AND column_name='expires_at' AND is_nullable='NO'"):
        run("ALTER TABLE recording MODIFY expires_at DATETIME(6) NULL")
    # 수신 중인 AI 통화의 유휴 기한만 유지한다. 이전 버전의 저장 기한은 해제한다.
    run("UPDATE recording SET expires_at=NULL WHERE expires_at IS NOT NULL AND "
        "(state IN ('READY','FAILED') OR (state='PENDING' AND uploaded_by IS NOT NULL))")
    migrate_recording_labels()
    migrate_desk()


def migrate_desk():
    """Apply the additive portal migration, including after a partial DDL run."""
    from app.db import SQL_DIR, db, new_id, one, rows, run

    # MySQL의 스키마 변경은 중간까지 적용된 채 끝날 수 있다. 현재 열 목록을 읽고
    # desk_schema.sql의 ADD COLUMN 중 이미 적용한 항목을 건너뛰어 재실행할 수 있게 한다.
    columns = {(row["tbl"], row["col"]) for row in rows(
        "SELECT table_name AS tbl,column_name AS col FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name IN ('staff_account','staff_team','support_case','voice_policy')")}
    indexes = {(row["tbl"], row["idx"]) for row in rows(
        "SELECT DISTINCT table_name AS tbl,index_name AS idx FROM information_schema.statistics "
        "WHERE table_schema=DATABASE() AND table_name IN ('ai_chat_turn','audit_event')")}
    for chunk in (SQL_DIR / "desk_schema.sql").read_text(encoding="utf-8").split(";"):
        statement = chunk.strip()
        if not statement:
            continue
        if statement.startswith("ALTER TABLE"):
            tokens = statement.split()
            if (tokens[2], tokens[5]) in columns:
                continue
        if statement.startswith("CREATE INDEX"):
            tokens = statement.split()
            if (tokens[4], tokens[2]) in indexes:
                continue
        run(statement)
    # 기본 팀 지정과 기존 상담원의 최초 배정을 한 트랜잭션에서 처리한다.
    if not one("SELECT team_id FROM staff_team WHERE is_default=TRUE"):
        with db().cursor() as cursor:
            cursor.execute(
                "INSERT INTO staff_team (team_id,name,is_default) VALUES (%s,%s,TRUE) "
                "ON DUPLICATE KEY UPDATE is_default=TRUE", (new_id(), "고객 상담팀"),
            )
            if cursor.rowcount:
                run("UPDATE staff_account a JOIN staff_team t ON t.is_default=TRUE "
                    "SET a.team_id=t.team_id WHERE a.role='AGENT'")
    db().commit()


def migrate_recording_labels():
    from app.db import db, one, run

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
