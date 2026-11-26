"""상담 티켓, 배정 이력과 직원 메모."""

from ..db import one, rows, run


def by_id(case_id, *, lock=False):
    sql = "SELECT * FROM visible_case WHERE case_id=%s"
    return one(sql + (" FOR UPDATE" if lock else ""), (case_id,))


def for_subject(subject_ref, channel):
    return rows(
        "SELECT * FROM visible_case WHERE support_subject_ref=%s AND channel=%s ORDER BY opened_at DESC,case_id",
        (subject_ref, channel),
    )


def insert_intake(case_id, channel, subject_ref, intake_id, inquiry_type):
    run(
        "INSERT INTO support_case (case_id, channel, support_subject_ref, intake_request_id,inquiry_type) "
        "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE case_id=case_id",
        (case_id, channel, subject_ref, intake_id, inquiry_type),
    )


def by_intake(intake_id):
    return one(
        "SELECT case_id,channel,support_subject_ref,deleted_at FROM support_case WHERE intake_request_id=%s",
        (intake_id,),
    )


def inquiry_status(case_id):
    return one("SELECT case_id,current_agent_id,closed_at FROM visible_case WHERE case_id=%s", (case_id,))


def closed_case(case_id):
    return one("SELECT case_id FROM visible_case WHERE case_id=%s AND closed_at IS NOT NULL", (case_id,))


def all_cases():
    return rows("SELECT * FROM visible_case ORDER BY opened_at DESC")


def assigned_cases(staff_id):
    return rows("SELECT * FROM visible_case WHERE current_agent_id=%s ORDER BY opened_at DESC", (staff_id,))


def waiting_cases():
    return rows(
        "SELECT c.case_id,c.channel,c.inquiry_type,c.opened_at, COALESCE((SELECT MAX(a.changed_at) FROM assignment_history a "
        "WHERE a.case_id=c.case_id AND a.new_agent_id IS NULL),c.opened_at) AS queued_at FROM visible_case c "
        "WHERE c.current_agent_id IS NULL AND c.closed_at IS NULL ORDER BY queued_at,c.case_id",
    )


def inbox(staff_id):
    return rows(
        "SELECT c.*, CASE WHEN c.channel='PHONE' THEN LEFT(p.inquiry,100) ELSE "
        "(SELECT LEFT(m.body,100) FROM chat_message m WHERE m.case_id=c.case_id AND m.body<>'' "
        "ORDER BY m.message_id DESC LIMIT 1) END AS preview "
        "FROM visible_case c LEFT JOIN phone_response p ON p.case_id=c.case_id "
        "WHERE c.current_agent_id=%s ORDER BY c.opened_at DESC, c.case_id",
        (staff_id,),
    )


def recently_closed(staff_id):
    return rows(
        "SELECT c.*, (SELECT r.recording_id FROM recording r WHERE r.case_id=c.case_id AND r.state='READY' "
        "AND r.expires_at>UTC_TIMESTAMP(6) ORDER BY r.stored_at DESC,r.recording_id LIMIT 1) AS clip_id "
        "FROM visible_case c WHERE c.current_agent_id=%s AND c.closed_at IS NOT NULL "
        "ORDER BY c.closed_at DESC,c.case_id LIMIT 6",
        (staff_id,),
    )


def desk_cases(*, include_recordings=False):
    recordings = ""
    if include_recordings:
        # 이력 화면만 녹취 건수와 편집 가능한 녹취의 존재 여부가 필요하다.
        recordings = (",(SELECT COUNT(*) FROM recording r WHERE r.case_id=c.case_id) AS recording_count, "
                      "EXISTS(SELECT 1 FROM recording r WHERE r.case_id=c.case_id AND r.expires_at>UTC_TIMESTAMP(6) "
                      "AND r.state NOT IN ('DELETE_PENDING','DESTROYED')) AS editable_recording")
    return rows(
        "SELECT c.case_id,c.channel,c.opened_at,c.closed_at,c.content_expires_at,c.current_agent_id,c.inquiry_type,s.login_id,s.display_name" + recordings +
        " FROM visible_case c LEFT JOIN staff_account s ON s.staff_id=c.current_agent_id "
        "ORDER BY c.opened_at DESC,c.case_id"
    )


def lock_open_assignments(staff_id):
    return rows(
        "SELECT * FROM visible_case WHERE current_agent_id=%s AND closed_at IS NULL ORDER BY case_id FOR UPDATE",
        (staff_id,),
    )


def log_assignment(case_id, previous_agent_id, new_agent_id, changed_by):
    run(
        "INSERT INTO assignment_history (case_id, previous_agent_id, new_agent_id, changed_by) VALUES (%s,%s,%s,%s)",
        (case_id, previous_agent_id, new_agent_id, changed_by),
    )


def set_assignee(case_id, agent_id):
    run("UPDATE support_case SET current_agent_id=%s WHERE case_id=%s", (agent_id, case_id))


def assignment_history(case_id):
    return rows(
        "SELECT a.changed_at, s.login_id FROM assignment_history a "
        "LEFT JOIN staff_account s ON s.staff_id=a.new_agent_id "
        "WHERE a.case_id=%s ORDER BY a.assignment_id",
        (case_id,),
    )


def set_category(case_id, inquiry_type):
    run("UPDATE support_case SET inquiry_type=%s WHERE case_id=%s", (inquiry_type, case_id))


def close(case_id, closed_at, result_note, outcome, expires_at):
    run(
        "UPDATE support_case SET closed_at=%s,result_note=%s,outcome=%s,content_expires_at=%s WHERE case_id=%s",
        (closed_at, result_note, outcome, expires_at, case_id),
    )


def staff_notes(case_id=None, *, agent_id=None):
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
    return one(
        "SELECT note_id,created_by,body FROM staff_note WHERE case_id=%s AND request_id=%s",
        (case_id, request_id),
    )


def add_note(case_id, request_id, author_id, body):
    return run(
        "INSERT INTO staff_note (case_id,request_id,created_by,body) VALUES (%s,%s,%s,%s)",
        (case_id, request_id, author_id, body),
    )


def note_for_edit(case_id, note_id):
    return one("SELECT body,updated_at FROM staff_note WHERE case_id=%s AND note_id=%s", (case_id, note_id))


def update_note(case_id, note_id, body):
    run("UPDATE staff_note SET body=%s WHERE case_id=%s AND note_id=%s", (body, case_id, note_id))


def clear_notes(case_id):
    run("UPDATE staff_note SET body=NULL WHERE case_id=%s AND body IS NOT NULL", (case_id,))


def clear_result(case_id):
    run("UPDATE support_case SET result_note=NULL WHERE case_id=%s", (case_id,))


def update_result(case_id, result_note):
    run("UPDATE support_case SET result_note=%s WHERE case_id=%s", (result_note, case_id))


def lock_for_deletion(case_id):
    return one("SELECT channel,deleted_at FROM support_case WHERE case_id=%s FOR UPDATE", (case_id,))


def mark_deleted(case_id):
    run(
        "UPDATE support_case SET deleted_at=UTC_TIMESTAMP(6),support_subject_ref=NULL,current_agent_id=NULL "
        "WHERE case_id=%s",
        (case_id,),
    )


def delete_notes(case_id):
    run("DELETE FROM staff_note WHERE case_id=%s", (case_id,))


def mark_content_purged(case_id):
    run("UPDATE support_case SET result_note=NULL, content_purged_at=UTC_TIMESTAMP(6) WHERE case_id=%s", (case_id,))


def due_for_purge():
    return rows(
        "SELECT case_id FROM support_case WHERE content_expires_at<=UTC_TIMESTAMP(6) AND content_purged_at IS NULL",
    )


def lock_retention(case_id):
    return one(
        "SELECT content_purged_at,content_expires_at FROM support_case WHERE case_id=%s FOR UPDATE",
        (case_id,),
    )


def seed_web_case(case_id, subject_ref, intake_id):
    run(
        "INSERT INTO support_case (case_id, channel, support_subject_ref, intake_request_id) "
        "VALUES (%s,'WEB',%s,%s) ON DUPLICATE KEY UPDATE case_id=case_id",
        (case_id, subject_ref, intake_id),
    )


def existing_case(case_id):
    return one("SELECT case_id FROM support_case WHERE case_id=%s", (case_id,))


def seed_phone_case(case_id, subject_ref, intake_id):
    run(
        "INSERT INTO support_case (case_id,channel,support_subject_ref,intake_request_id) VALUES (%s,'PHONE',%s,%s)",
        (case_id, subject_ref, intake_id),
    )
