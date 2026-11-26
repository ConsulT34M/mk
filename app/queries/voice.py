"""전화 응대, AI 통화 세션과 요약 작업."""

from ..db import one, rows, run

# 목록 조회, 요약 작업 선택, 결과 저장에 같은 조건을 사용한다. READY이고 기한 안에 있는 AI 녹취가
# 대상이다. 티켓을 LEFT JOIN해 인계 전 통화도 포함하고, 연결된 티켓이 삭제된 경우는 제외한다.
RECORDINGS = " FROM recording r LEFT JOIN support_case c ON c.case_id=r.case_id "
AVAILABLE = "r.uploaded_by IS NULL AND r.state='READY' AND r.expires_at>UTC_TIMESTAMP(6) AND c.deleted_at IS NULL"
SUMMARY_JOIN = " LEFT JOIN ai_call_summary s ON s.recording_id=r.recording_id "
# 같은 DB를 처리하는 작업자들이 공유할 MySQL 잠금 이름이다. DB 이름을 반영해 설치별로 구분한다.
LOCK_NAME = "CONCAT('mk:ai-call:',MD5(DATABASE()))"


def policy():
    return one("SELECT * FROM voice_policy WHERE policy_id=1")


def update_policy(enabled, offer_handoff, instructions):
    run(
        "UPDATE voice_policy SET enabled=%s,offer_handoff=%s,instructions=%s WHERE policy_id=1",
        (enabled, offer_handoff, instructions),
    )


def create_session(voice_id, owner_hash, subject_ref):
    run(
        "INSERT INTO voice_session (voice_id,owner_hash,subject_ref) VALUES (%s,%s,%s)",
        (voice_id, owner_hash, subject_ref),
    )


def owned_session(voice_id, owner_hash):
    return one("SELECT subject_ref FROM voice_session WHERE voice_id=%s AND owner_hash=%s", (voice_id, owner_hash))


def end_session(voice_id):
    run("DELETE FROM voice_session WHERE voice_id=%s", (voice_id,))


def create_phone_response(case_id):
    run("INSERT INTO phone_response (case_id) VALUES (%s)", (case_id,))


def phone_response(case_id):
    return one(
        "SELECT p.call_at,p.consent_state,p.inquiry,p.guidance,p.followup_required,c.call_direction,c.talk_seconds "
        "FROM phone_response p JOIN support_case c USING(case_id) WHERE p.case_id=%s",
        (case_id,),
    )


def recording_consent(case_id):
    return one("SELECT consent_state FROM phone_response WHERE case_id=%s", (case_id,))


def update_phone_response(case_id, edits, metrics):
    response_fields = {"call_at", "inquiry", "guidance", "followup_required", "consent_state", "consent_by", "consent_at"}
    for table, values, allowed in (("phone_response", edits, response_fields),
                                   ("support_case", metrics, {"call_direction", "talk_seconds"})):
        if not values:
            continue
        if values.keys() - allowed:
            raise ValueError("Unexpected phone response column")
        columns = ", ".join(f"{key}=%s" for key in values)
        run(f"UPDATE {table} SET {columns} WHERE case_id=%s", (*values.values(), case_id))


def clear_phone_response(case_id):
    run(
        "UPDATE phone_response SET call_at=NULL, consent_state='UNASKED', consent_by=NULL, consent_at=NULL, "
        "inquiry=NULL, guidance=NULL, followup_required=NULL WHERE case_id=%s",
        (case_id,),
    )


def delete_phone_response(case_id):
    run("DELETE FROM phone_response WHERE case_id=%s", (case_id,))


def list_calls():
    return rows(
        "SELECT r.recording_id,r.case_id,r.stored_at,r.expires_at,r.label,s.state AS summary_state,"
        "s.summary,s.error_message" + RECORDINGS + SUMMARY_JOIN + "WHERE " + AVAILABLE +
        " ORDER BY r.stored_at DESC,r.recording_id",
    )


def add_summary(recording_id):
    run("INSERT INTO ai_call_summary (recording_id) VALUES (%s)", (recording_id,))


def summary_state(recording_id):
    return one("SELECT state FROM ai_call_summary WHERE recording_id=%s", (recording_id,))


def queue_summary(recording_id):
    run(
        "INSERT INTO ai_call_summary (recording_id) VALUES (%s) ON DUPLICATE KEY UPDATE state='QUEUED',error_message=NULL",
        (recording_id,),
    )


def acquire_summary_lock():
    return one("SELECT GET_LOCK(" + LOCK_NAME + ",0) AS acquired")


def next_summary():
    return one(
        "SELECT r.recording_id,r.object_key,r.storage_bucket" + RECORDINGS + SUMMARY_JOIN +
        "WHERE s.state IN ('QUEUED','PROCESSING') AND " + AVAILABLE + " ORDER BY r.expires_at,r.recording_id LIMIT 1",
    )


def mark_summary_processing(recording_id):
    run("UPDATE ai_call_summary SET state='PROCESSING',error_message=NULL WHERE recording_id=%s", (recording_id,))


def available_for_summary(recording_id):
    return one("SELECT 1" + RECORDINGS + "WHERE r.recording_id=%s AND " + AVAILABLE, (recording_id,))


def finish_summary(recording_id, state, summary, error_message):
    run(
        "UPDATE ai_call_summary s JOIN recording r ON r.recording_id=s.recording_id "
        "LEFT JOIN support_case c ON c.case_id=r.case_id SET s.state=%s,s.summary=%s,s.error_message=%s "
        "WHERE s.recording_id=%s AND " + AVAILABLE,
        (state, summary, error_message, recording_id),
    )


def release_summary_lock():
    return one("SELECT RELEASE_LOCK(" + LOCK_NAME + ")")


def delete_expired_summaries():
    run(
        "DELETE s FROM ai_call_summary s JOIN recording r ON r.recording_id=s.recording_id "
        "WHERE r.state IN ('DELETE_PENDING','DESTROYED','FAILED') OR (r.state='READY' AND r.expires_at<=UTC_TIMESTAMP(6))",
    )
