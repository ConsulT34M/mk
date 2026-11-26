"""녹취 예약, 상태 변경과 보관 기한 관리."""

from ..db import one, rows, run


def for_case(case_id):
    return rows("SELECT * FROM recording WHERE case_id=%s ORDER BY expires_at DESC", (case_id,))


def case_history(case_id):
    return rows(
        "SELECT r.recording_id,r.state,r.stored_at,r.expires_at,r.size_bytes,r.uploaded_by IS NULL AS ai,"
        "(r.expires_at>UTC_TIMESTAMP(6) AND r.state NOT IN ('DELETE_PENDING','DESTROYED')) AS editable,"
        "CASE WHEN r.expires_at>UTC_TIMESTAMP(6) AND r.state NOT IN ('DELETE_PENDING','DESTROYED') THEN r.label END AS label "
        "FROM recording r "
        "WHERE r.case_id=%s ORDER BY r.expires_at DESC,r.recording_id",
        (case_id,),
    )


def lock_upload(case_id, request_id):
    return one(
        "SELECT * FROM recording WHERE case_id=%s AND upload_request_id=%s FOR UPDATE",
        (case_id, request_id),
    )


def reserve_upload(recording_id, case_id, request_id, uploaded_by, object_key, storage_bucket, size_bytes, sha256, expires_at):
    run(
        "INSERT INTO recording (recording_id, case_id, upload_request_id, uploaded_by, state, object_key, "
        "storage_bucket, size_bytes, sha256, expires_at) VALUES (%s,%s,%s,%s,'PENDING',%s,%s,%s,%s,%s)",
        (recording_id, case_id, request_id, uploaded_by, object_key, storage_bucket, size_bytes, sha256, expires_at),
    )


def lock_recording(recording_id):
    return one("SELECT * FROM recording WHERE recording_id=%s FOR UPDATE", (recording_id,))


def lock_case_recording(case_id, recording_id):
    return one("SELECT * FROM recording WHERE recording_id=%s AND case_id=%s FOR UPDATE", (recording_id, case_id))


def mark_failed(recording_id):
    run("UPDATE recording SET state='FAILED' WHERE recording_id=%s", (recording_id,))


def mark_ready(recording_id, expires_at):
    run(
        "UPDATE recording SET state='READY', stored_at=UTC_TIMESTAMP(6), expires_at=%s WHERE recording_id=%s",
        (expires_at, recording_id),
    )


def reserve_voice_recording(recording_id, object_key, storage_bucket, expires_at):
    run(
        "INSERT INTO recording (recording_id,upload_request_id,state,object_key,storage_bucket,size_bytes,expires_at) "
        "VALUES (%s,%s,'PENDING',%s,%s,0,%s)",
        (recording_id, recording_id, object_key, storage_bucket, expires_at),
    )


def record_progress(recording_id, size_bytes, expires_at):
    run(
        "UPDATE recording SET size_bytes=%s,expires_at=%s WHERE recording_id=%s",
        (size_bytes, expires_at, recording_id),
    )


def discard_empty_recording(recording_id):
    run(
        "UPDATE recording SET state='DESTROYED',object_key=NULL,storage_bucket=NULL,size_bytes=NULL,label=NULL,destroyed_at=UTC_TIMESTAMP(6) "
        "WHERE recording_id=%s",
        (recording_id,),
    )


def finish_voice_recording(recording_id, size_bytes, sha256, expires_at):
    run(
        "UPDATE recording SET state='READY',size_bytes=%s,sha256=%s,stored_at=UTC_TIMESTAMP(6),expires_at=%s "
        "WHERE recording_id=%s",
        (size_bytes, sha256, expires_at, recording_id),
    )


def link_voice_case(recording_id, case_id):
    run("UPDATE recording SET case_id=%s WHERE recording_id=%s AND uploaded_by IS NULL", (case_id, recording_id))


def lock_voice_recording(recording_id):
    return one("SELECT * FROM recording WHERE recording_id=%s AND uploaded_by IS NULL FOR UPDATE", (recording_id,))


def lock_summary_source(recording_id):
    return one(
        "SELECT r.case_id,r.state,r.expires_at,c.deleted_at FROM recording r "
        "LEFT JOIN support_case c ON c.case_id=r.case_id WHERE r.recording_id=%s AND r.uploaded_by IS NULL FOR UPDATE",
        (recording_id,),
    )


def set_label(recording_id, label):
    run("UPDATE recording SET label=%s WHERE recording_id=%s", (label, recording_id))


def mark_case_for_deletion(case_id):
    run(
        "UPDATE recording SET state='DELETE_PENDING' WHERE case_id=%s AND state IN ('PENDING','FAILED','READY')",
        (case_id,),
    )


def mark_for_deletion(recording_id):
    run("UPDATE recording SET state='DELETE_PENDING' WHERE recording_id=%s", (recording_id,))


def due_for_purge():
    return rows(
        "SELECT recording_id FROM recording WHERE state='DELETE_PENDING' "
        "OR (state<>'DESTROYED' AND expires_at<=UTC_TIMESTAMP(6))",
    )


def mark_destroyed(recording_id):
    run(
        "UPDATE recording SET state='DESTROYED', object_key=NULL, storage_bucket=NULL, size_bytes=NULL, sha256=NULL, label=NULL, "
        "destroyed_at=UTC_TIMESTAMP(6) WHERE recording_id=%s",
        (recording_id,),
    )
