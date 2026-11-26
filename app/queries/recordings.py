"""녹취 예약, 조회와 상태 변경."""

from ..db import one, rows, run


def for_case(case_id, agent_id):
    return rows(
        "SELECT r.recording_id,r.state,r.uploaded_by,r.label,s.summary FROM recording r "
        "JOIN visible_case c ON c.case_id=r.case_id "
        "LEFT JOIN ai_call_summary s ON s.recording_id=r.recording_id AND s.state='READY' "
        "AND r.uploaded_by IS NULL AND r.state='READY' "
        "WHERE r.case_id=%s AND c.current_agent_id=%s ORDER BY r.stored_at DESC,r.recording_id",
        (case_id, agent_id),
    )


def case_history(case_id):
    # 상담 이력에 표시할 녹취 상태와 편집 가능 여부를 조회한다.
    return rows(
        "SELECT r.recording_id,r.state,r.stored_at,r.size_bytes,r.uploaded_by IS NULL AS ai,"
        "(r.state NOT IN ('DELETE_PENDING','DESTROYED')) AS editable,"
        "CASE WHEN r.state NOT IN ('DELETE_PENDING','DESTROYED') THEN r.label END AS label "
        "FROM recording r "
        "WHERE r.case_id=%s ORDER BY r.stored_at DESC,r.recording_id",
        (case_id,),
    )


def lock_upload(case_id, request_id):
    # 같은 요청으로 등록된 녹취를 찾아 잠근다.
    return one(
        "SELECT * FROM recording WHERE case_id=%s AND upload_request_id=%s FOR UPDATE",
        (case_id, request_id),
    )


def reserve_upload(recording_id, case_id, request_id, uploaded_by, object_key, storage_bucket, size_bytes, sha256):
    # 직원 녹취 업로드를 대기 상태로 등록한다.
    run(
        "INSERT INTO recording (recording_id, case_id, upload_request_id, uploaded_by, state, object_key, "
        "storage_bucket, size_bytes, sha256) VALUES (%s,%s,%s,%s,'PENDING',%s,%s,%s,%s)",
        (recording_id, case_id, request_id, uploaded_by, object_key, storage_bucket, size_bytes, sha256),
    )


def lock_recording(recording_id):
    # 녹취를 ID로 조회하고 잠근다.
    return one("SELECT * FROM recording WHERE recording_id=%s FOR UPDATE", (recording_id,))


def lock_case_recording(case_id, recording_id):
    # 해당 상담에 속한 녹취를 찾아 잠근다.
    return one("SELECT * FROM recording WHERE recording_id=%s AND case_id=%s FOR UPDATE", (recording_id, case_id))


def mark_failed(recording_id):
    # 녹취를 실패 상태로 표시한다.
    run("UPDATE recording SET state='FAILED' WHERE recording_id=%s", (recording_id,))


def mark_ready(recording_id):
    run(
        "UPDATE recording SET state='READY',stored_at=UTC_TIMESTAMP(6),expires_at=NULL WHERE recording_id=%s",
        (recording_id,),
    )


def reserve_voice_recording(recording_id, object_key, storage_bucket, expires_at):
    # AI 통화에서 받을 녹취를 대기 상태로 등록한다.
    run(
        "INSERT INTO recording (recording_id,upload_request_id,state,object_key,storage_bucket,size_bytes,expires_at) "
        "VALUES (%s,%s,'PENDING',%s,%s,0,%s)",
        (recording_id, recording_id, object_key, storage_bucket, expires_at),
    )


def record_progress(recording_id, size_bytes, expires_at, started_at):
    # 수신한 음성 크기와 만료 시각을 갱신한다.
    run(
        "UPDATE recording SET size_bytes=%s,expires_at=%s,call_started_at=COALESCE(call_started_at,%s) WHERE recording_id=%s",
        (size_bytes, expires_at, started_at, recording_id),
    )


def discard_empty_recording(recording_id):
    # 음성이 없는 녹취를 파기 처리한다.
    run(
        "UPDATE recording SET state='DESTROYED',object_key=NULL,storage_bucket=NULL,size_bytes=NULL,label=NULL,destroyed_at=UTC_TIMESTAMP(6) "
        "WHERE recording_id=%s",
        (recording_id,),
    )


def finish_voice_recording(recording_id, size_bytes, sha256):
    run(
        "UPDATE recording SET state='READY',size_bytes=%s,sha256=%s,stored_at=UTC_TIMESTAMP(6),expires_at=NULL "
        "WHERE recording_id=%s",
        (size_bytes, sha256, recording_id),
    )


def link_voice_case(recording_id, case_id):
    # 최초 인계 때 녹취와 응대 기록이 같은 통화 시작 시각을 사용한다.
    run(
        "UPDATE recording r JOIN phone_response p ON p.case_id=%s "
        "SET r.case_id=p.case_id,p.call_at=COALESCE(r.call_started_at,p.call_at),"
        "r.call_started_at=COALESCE(r.call_started_at,p.call_at) WHERE r.recording_id=%s AND r.uploaded_by IS NULL",
        (case_id, recording_id),
    )


def lock_voice_recording(recording_id):
    # AI 통화 녹취를 찾아 잠근다.
    return one("SELECT * FROM recording WHERE recording_id=%s AND uploaded_by IS NULL FOR UPDATE", (recording_id,))


def lock_summary_source(recording_id):
    # 요약할 AI 녹취를 잠그고 연결된 상담의 삭제 여부를 조회한다.
    return one(
        "SELECT r.case_id,r.state,c.deleted_at FROM recording r "
        "LEFT JOIN support_case c ON c.case_id=r.case_id WHERE r.recording_id=%s AND r.uploaded_by IS NULL FOR UPDATE",
        (recording_id,),
    )


def set_label(recording_id, label):
    # 녹취의 표시 이름을 바꾼다.
    run("UPDATE recording SET label=%s WHERE recording_id=%s", (label, recording_id))


def mark_case_for_deletion(case_id):
    # 해당 상담의 녹취를 삭제 대기로 전환한다.
    run(
        "UPDATE recording SET state='DELETE_PENDING' WHERE case_id=%s AND state IN ('PENDING','FAILED','READY')",
        (case_id,),
    )


def mark_for_deletion(recording_id):
    # 녹취 한 건을 삭제 대기로 전환한다.
    run("UPDATE recording SET state='DELETE_PENDING' WHERE recording_id=%s", (recording_id,))


def due_for_purge():
    # 삭제가 요청된 녹취와 수신이 멈춰 WAV로 완성해야 하는 AI 통화만 찾는다.
    return rows(
        "SELECT recording_id FROM recording WHERE state='DELETE_PENDING' "
        "OR (state='PENDING' AND uploaded_by IS NULL AND expires_at<=UTC_TIMESTAMP(6))",
    )


def mark_destroyed(recording_id):
    # 파일 정보를 비우고 녹취 파기 시각을 기록한다.
    run(
        "UPDATE recording SET state='DESTROYED', object_key=NULL, storage_bucket=NULL, size_bytes=NULL, sha256=NULL, label=NULL, "
        "call_started_at=NULL,destroyed_at=UTC_TIMESTAMP(6) WHERE recording_id=%s",
        (recording_id,),
    )
