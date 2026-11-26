"""상담 티켓, 배정 이력과 직원 메모."""

from ..db import one, rows, run


def by_id(case_id, *, lock=False):
    # 상담 ID로 조회하고 필요하면 행을 잠근다.
    sql = "SELECT * FROM visible_case WHERE case_id=%s"
    return one(sql + (" FOR UPDATE" if lock else ""), (case_id,))


def for_subject(subject_ref, channel):
    # 고객의 채널별 상담을 최근 접수순으로 조회한다.
    return rows(
        "SELECT * FROM visible_case WHERE support_subject_ref=%s AND channel=%s ORDER BY opened_at DESC,case_id",
        (subject_ref, channel),
    )


def insert_intake(case_id, channel, subject_ref, intake_id, inquiry_type):
    # 같은 접수 요청은 한 건만 등록한다.
    run(
        "INSERT INTO support_case (case_id, channel, support_subject_ref, intake_request_id,inquiry_type) "
        "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE case_id=case_id",
        (case_id, channel, subject_ref, intake_id, inquiry_type),
    )


def by_intake(intake_id):
    # 접수 요청 ID로 기존 상담과 삭제 여부를 확인한다.
    return one(
        "SELECT case_id,channel,support_subject_ref,deleted_at FROM support_case WHERE intake_request_id=%s",
        (intake_id,),
    )


def inquiry_status(case_id):
    # 문의 화면에 표시할 담당자와 종결 시각을 조회한다.
    return one("SELECT case_id,current_agent_id,closed_at FROM visible_case WHERE case_id=%s", (case_id,))


def closed_case(case_id):
    # 해당 상담이 종결됐는지 확인한다.
    return one("SELECT case_id FROM visible_case WHERE case_id=%s AND closed_at IS NOT NULL", (case_id,))


def all_cases():
    # 삭제되지 않은 상담을 최근 접수순으로 조회한다.
    return rows("SELECT * FROM visible_case ORDER BY opened_at DESC")


def assigned_cases(staff_id):
    # 직원에게 배정된 상담을 최근 접수순으로 조회한다.
    return rows("SELECT * FROM visible_case WHERE current_agent_id=%s ORDER BY opened_at DESC", (staff_id,))


def waiting_cases():
    # 미배정 상담을 대기 시작 시각순으로 조회한다.
    return rows(
        "SELECT c.case_id,c.channel,c.inquiry_type,c.opened_at, COALESCE((SELECT MAX(a.changed_at) FROM assignment_history a "
        "WHERE a.case_id=c.case_id AND a.new_agent_id IS NULL),c.opened_at) AS queued_at FROM visible_case c "
        "WHERE c.current_agent_id IS NULL AND c.closed_at IS NULL ORDER BY queued_at,c.case_id",
    )


def inbox(staff_id):
    # 담당 상담과 최근 메시지 또는 전화 문의 미리보기를 조회한다.
    return rows(
        "SELECT c.*, CASE WHEN c.channel='PHONE' THEN LEFT(p.inquiry,100) ELSE "
        "(SELECT LEFT(m.body,100) FROM chat_message m WHERE m.case_id=c.case_id AND m.body<>'' "
        "ORDER BY m.message_id DESC LIMIT 1) END AS preview "
        "FROM visible_case c LEFT JOIN phone_response p ON p.case_id=c.case_id "
        "WHERE c.current_agent_id=%s ORDER BY COALESCE(c.closed_at,c.opened_at) DESC,c.case_id",
        (staff_id,),
    )


def desk_cases(*, include_recordings=False):
    # 데스크 상담 목록을 조회하고 이력 화면에는 녹취 정보를 덧붙인다.
    recordings = ""
    if include_recordings:
        recordings = (",(SELECT COUNT(*) FROM recording r WHERE r.case_id=c.case_id) AS recording_count, "
                      "EXISTS(SELECT 1 FROM recording r WHERE r.case_id=c.case_id "
                      "AND r.state NOT IN ('DELETE_PENDING','DESTROYED')) AS editable_recording")
    return rows(
        "SELECT c.case_id,c.channel,c.opened_at,c.closed_at,c.content_expires_at,c.current_agent_id,c.inquiry_type,s.login_id,s.display_name" + recordings +
        " FROM visible_case c LEFT JOIN staff_account s ON s.staff_id=c.current_agent_id "
        "ORDER BY c.opened_at DESC,c.case_id"
    )


def lock_open_assignments(staff_id):
    # 재배정할 수 있도록 직원의 미종결 상담을 잠근다.
    return rows(
        "SELECT * FROM visible_case WHERE current_agent_id=%s AND closed_at IS NULL ORDER BY case_id FOR UPDATE",
        (staff_id,),
    )


def log_assignment(case_id, previous_agent_id, new_agent_id, changed_by):
    # 이전 담당자와 새 담당자를 배정 이력에 남긴다.
    run(
        "INSERT INTO assignment_history (case_id, previous_agent_id, new_agent_id, changed_by) VALUES (%s,%s,%s,%s)",
        (case_id, previous_agent_id, new_agent_id, changed_by),
    )


def set_assignee(case_id, agent_id):
    # 상담의 현재 담당자를 지정하거나 해제한다.
    run("UPDATE support_case SET current_agent_id=%s WHERE case_id=%s", (agent_id, case_id))


def assignment_history(case_id):
    # 상담의 담당자 변경 이력을 등록 순서대로 조회한다.
    return rows(
        "SELECT a.changed_at, s.login_id FROM assignment_history a "
        "LEFT JOIN staff_account s ON s.staff_id=a.new_agent_id "
        "WHERE a.case_id=%s ORDER BY a.assignment_id",
        (case_id,),
    )


def set_category(case_id, inquiry_type):
    # 상담의 문의 유형을 변경한다.
    run("UPDATE support_case SET inquiry_type=%s WHERE case_id=%s", (inquiry_type, case_id))


def close(case_id, closed_at, result_note, outcome, expires_at):
    # 종결 시각, 처리 결과와 상담 내용 보관 기한을 저장한다.
    run(
        "UPDATE support_case SET closed_at=%s,result_note=%s,outcome=%s,content_expires_at=%s WHERE case_id=%s",
        (closed_at, result_note, outcome, expires_at, case_id),
    )


def staff_notes(case_id=None, *, agent_id=None):
    # 본문을 볼 수 있는 상담의 직원 메모를 상담·담당자별로 조회한다.
    sql = (
        "SELECT n.note_id,n.case_id,n.body,n.created_at,n.updated_at,a.login_id FROM staff_note n "
        "JOIN staff_account a ON a.staff_id=n.created_by JOIN visible_case c ON c.case_id=n.case_id "
        "WHERE n.body IS NOT NULL AND (c.content_expires_at IS NULL OR c.content_expires_at>UTC_TIMESTAMP(6))"
    )
    args = []
    if case_id is not None:
        sql += " AND n.case_id=%s"
        args.append(case_id)
    if agent_id is not None:
        sql += " AND c.current_agent_id=%s"
        args.append(agent_id)
    return rows(sql + " ORDER BY n.note_id DESC", args)


def note_by_request(case_id, request_id):
    # 같은 요청으로 등록된 상담 메모를 찾는다.
    return one(
        "SELECT note_id,created_by,body FROM staff_note WHERE case_id=%s AND request_id=%s",
        (case_id, request_id),
    )


def add_note(case_id, request_id, author_id, body):
    # 직원 메모를 저장하고 메모 ID를 반환한다.
    return run(
        "INSERT INTO staff_note (case_id,request_id,created_by,body) VALUES (%s,%s,%s,%s)",
        (case_id, request_id, author_id, body),
    )


def note_for_edit(case_id, note_id):
    # 수정할 메모의 본문과 마지막 변경 시각을 조회한다.
    return one("SELECT body,updated_at FROM staff_note WHERE case_id=%s AND note_id=%s", (case_id, note_id))


def update_note(case_id, note_id, body):
    # 직원 메모의 본문을 수정한다.
    run("UPDATE staff_note SET body=%s WHERE case_id=%s AND note_id=%s", (body, case_id, note_id))


def clear_notes(case_id):
    # 상담에 달린 직원 메모의 본문을 비운다.
    run("UPDATE staff_note SET body=NULL WHERE case_id=%s AND body IS NOT NULL", (case_id,))


def clear_result(case_id):
    run("UPDATE support_case SET result_note=NULL,outcome=NULL WHERE case_id=%s", (case_id,))


def update_result(case_id, result_note, outcome):
    run("UPDATE support_case SET result_note=%s,outcome=%s WHERE case_id=%s", (result_note, outcome, case_id))


def lock_for_deletion(case_id):
    # 삭제할 상담을 잠그고 기존 삭제 여부를 확인한다.
    return one("SELECT channel,deleted_at FROM support_case WHERE case_id=%s FOR UPDATE", (case_id,))


def mark_deleted(case_id):
    # 상담을 삭제 표시하고 고객·담당자 연결을 해제한다.
    run(
        "UPDATE support_case SET deleted_at=UTC_TIMESTAMP(6),support_subject_ref=NULL,current_agent_id=NULL "
        "WHERE case_id=%s",
        (case_id,),
    )


def delete_notes(case_id):
    # 상담에 달린 직원 메모를 삭제한다.
    run("DELETE FROM staff_note WHERE case_id=%s", (case_id,))


def mark_content_purged(case_id):
    # 결과 메모를 비우고 상담 내용의 파기 시각을 기록한다.
    run("UPDATE support_case SET result_note=NULL, content_purged_at=UTC_TIMESTAMP(6) WHERE case_id=%s", (case_id,))


def due_for_purge():
    # 보관 기한이 지나 파기할 상담을 찾는다.
    return rows(
        "SELECT case_id FROM support_case WHERE content_expires_at<=UTC_TIMESTAMP(6) AND content_purged_at IS NULL",
    )


def lock_retention(case_id):
    # 상담을 잠그고 보관 기한과 파기 여부를 확인한다.
    return one(
        "SELECT content_purged_at,content_expires_at FROM support_case WHERE case_id=%s FOR UPDATE",
        (case_id,),
    )


def seed_web_case(case_id, subject_ref, intake_id):
    # 데모용 웹 상담을 중복 없이 등록한다.
    run(
        "INSERT INTO support_case (case_id, channel, support_subject_ref, intake_request_id) "
        "VALUES (%s,'WEB',%s,%s) ON DUPLICATE KEY UPDATE case_id=case_id",
        (case_id, subject_ref, intake_id),
    )


def existing_case(case_id):
    # 삭제 표시된 상담까지 포함해 ID가 있는지 확인한다.
    return one("SELECT case_id FROM support_case WHERE case_id=%s", (case_id,))


def seed_phone_case(case_id, subject_ref, intake_id):
    # 데모용 전화 상담을 등록한다.
    run(
        "INSERT INTO support_case (case_id,channel,support_subject_ref,intake_request_id) VALUES (%s,'PHONE',%s,%s)",
        (case_id, subject_ref, intake_id),
    )
