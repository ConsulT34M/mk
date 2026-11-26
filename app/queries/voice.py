"""전화 응대, AI 통화 세션과 요약 작업."""

from ..db import one, rows, run

# 재생 가능한 AI 녹취에는 인계 전 통화도 포함한다. 삭제된 상담은 제외한다.
RECORDINGS = " FROM recording r LEFT JOIN support_case c ON c.case_id=r.case_id "
AVAILABLE = "r.uploaded_by IS NULL AND r.state='READY' AND c.deleted_at IS NULL"
SUMMARY_JOIN = " LEFT JOIN ai_call_summary s ON s.recording_id=r.recording_id "
# 같은 DB의 요약 작업자가 공유하는 잠금 이름.
LOCK_NAME = "CONCAT('mk:ai-call:',MD5(DATABASE()))"


def policy():
    # AI 통화의 운영 설정을 조회한다.
    return one("SELECT * FROM voice_policy WHERE policy_id=1")


def update_policy(enabled, offer_handoff, instructions, greeting=None, ringback_enabled=None):
    run(
        "UPDATE voice_policy SET enabled=%s,offer_handoff=%s,instructions=%s,"
        "greeting=COALESCE(%s,greeting),ringback_enabled=COALESCE(%s,ringback_enabled) WHERE policy_id=1",
        (enabled, offer_handoff, instructions, greeting, ringback_enabled),
    )


def create_session(voice_id, owner_hash, subject_ref):
    # 방문자와 연결된 AI 통화 세션을 만든다.
    run(
        "INSERT INTO voice_session (voice_id,owner_hash,subject_ref) VALUES (%s,%s,%s)",
        (voice_id, owner_hash, subject_ref),
    )


def extend_rkx_session(owner, expires):
    run("INSERT INTO customer_session (token_hash,expires_at) VALUES (%s,%s) "
        "ON DUPLICATE KEY UPDATE expires_at=GREATEST(expires_at,%s)", (owner, expires, expires))


def log_rkx_start(voice_id, subject):
    run("INSERT INTO rkx_voice_call (voice_id,subject_ref) VALUES (%s,%s)", (voice_id, subject))


def log_rkx_connected(voice_id):
    run("UPDATE rkx_voice_call SET connected_at=COALESCE(connected_at,UTC_TIMESTAMP(6)) WHERE voice_id=%s", (voice_id,))


def log_rkx_end(voice_id):
    run("UPDATE rkx_voice_call SET ended_at=UTC_TIMESTAMP(6) WHERE voice_id=%s", (voice_id,))


def rkx_calls():
    return rows("SELECT c.*, (s.voice_id IS NOT NULL AND o.expires_at>UTC_TIMESTAMP(6)) AS active "
                "FROM rkx_voice_call c LEFT JOIN voice_session s USING(voice_id) "
                "LEFT JOIN customer_session o ON o.token_hash=s.owner_hash ORDER BY requested_at DESC LIMIT 100")


def owned_session(voice_id, owner_hash):
    # 해당 방문자가 시작한 통화의 고객 참조값을 조회한다.
    return one("SELECT subject_ref FROM voice_session WHERE voice_id=%s AND owner_hash=%s", (voice_id, owner_hash))


def end_session(voice_id):
    # 종료된 통화의 세션을 삭제한다.
    run("DELETE FROM voice_session WHERE voice_id=%s", (voice_id,))


def create_phone_response(case_id, call_at=None):
    run("INSERT INTO phone_response (case_id,call_at) VALUES (%s,%s)", (case_id, call_at))


def phone_response(case_id):
    # 전화 응대 내용, 녹취 동의 상태와 통화 정보를 조회한다.
    return one(
        "SELECT p.call_at,p.consent_state,p.inquiry,p.guidance,p.followup_required,c.call_direction,c.talk_seconds "
        "FROM phone_response p JOIN support_case c USING(case_id) WHERE p.case_id=%s",
        (case_id,),
    )


def recording_consent(case_id):
    # 전화 상담의 녹취 동의 상태를 확인한다.
    return one("SELECT consent_state FROM phone_response WHERE case_id=%s", (case_id,))


def update_phone_response(case_id, edits, metrics):
    # 요청에 포함된 응대 내용과 통화 정보만 갱신한다.
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
    # 응대 내용을 비우고 녹취 동의 상태를 초기화한다.
    run(
        "UPDATE phone_response SET call_at=NULL, consent_state='UNASKED', consent_by=NULL, consent_at=NULL, "
        "inquiry=NULL, guidance=NULL, followup_required=NULL WHERE case_id=%s",
        (case_id,),
    )


def delete_phone_response(case_id):
    # 해당 상담의 전화 응대 기록을 삭제한다.
    run("DELETE FROM phone_response WHERE case_id=%s", (case_id,))


def list_calls():
    # 재생 가능한 AI 녹취와 요약 상태를 최신순으로 조회한다.
    return rows(
        "SELECT r.recording_id,r.case_id,r.call_started_at,r.stored_at,r.label,s.state AS summary_state,"
        "s.summary,s.error_message" + RECORDINGS + SUMMARY_JOIN + "WHERE " + AVAILABLE +
        " ORDER BY r.stored_at DESC,r.recording_id",
    )


def add_summary(recording_id):
    # 녹취의 첫 요약 작업을 등록한다.
    run("INSERT INTO ai_call_summary (recording_id) VALUES (%s)", (recording_id,))


def summary_state(recording_id):
    # 녹취의 요약 진행 상태를 조회한다.
    return one("SELECT state FROM ai_call_summary WHERE recording_id=%s", (recording_id,))


def queue_summary(recording_id):
    # 요약 작업을 새로 등록하거나 대기 상태로 되돌린다.
    run(
        "INSERT INTO ai_call_summary (recording_id) VALUES (%s) ON DUPLICATE KEY UPDATE state='QUEUED',error_message=NULL",
        (recording_id,),
    )


def acquire_summary_lock():
    # 한 작업자만 요약하도록 대기 없이 DB 잠금을 시도한다.
    return one("SELECT GET_LOCK(" + LOCK_NAME + ",0) AS acquired")


def next_summary():
    # 먼저 저장된 녹취부터 요약한다.
    return one(
        "SELECT r.recording_id,r.object_key,r.storage_bucket" + RECORDINGS + SUMMARY_JOIN +
        "WHERE s.state IN ('QUEUED','PROCESSING') AND " + AVAILABLE + " ORDER BY r.stored_at,r.recording_id LIMIT 1",
    )


def mark_summary_processing(recording_id):
    # 요약을 처리 중으로 바꾸고 이전 오류를 비운다.
    run("UPDATE ai_call_summary SET state='PROCESSING',error_message=NULL WHERE recording_id=%s", (recording_id,))


def available_for_summary(recording_id):
    # 녹취가 아직 요약 가능한지 확인한다.
    return one("SELECT 1" + RECORDINGS + "WHERE r.recording_id=%s AND " + AVAILABLE, (recording_id,))


def finish_summary(recording_id, state, summary, error_message):
    # 유효한 녹취에만 요약 결과나 오류를 저장한다.
    run(
        "UPDATE ai_call_summary s JOIN recording r ON r.recording_id=s.recording_id "
        "LEFT JOIN support_case c ON c.case_id=r.case_id SET s.state=%s,s.summary=%s,s.error_message=%s "
        "WHERE s.recording_id=%s AND " + AVAILABLE,
        (state, summary, error_message, recording_id),
    )


def release_summary_lock():
    # 요약 작업자의 DB 잠금을 해제한다.
    return one("SELECT RELEASE_LOCK(" + LOCK_NAME + ")")


def delete_unavailable_summaries():
    run(
        "DELETE s FROM ai_call_summary s JOIN recording r ON r.recording_id=s.recording_id "
        "WHERE r.state IN ('DELETE_PENDING','DESTROYED','FAILED')",
    )
