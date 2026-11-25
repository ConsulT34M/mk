import hashlib
import io
import os
import struct
import wave
from datetime import timedelta

from flask import Blueprint, current_app, g, request

from .common import ApiError, assigned_case, channel, deadline, uuid_field, writable
from .db import audit, db, new_id, one, rows, run, stamp, utcnow
from . import recording_store
from .voice_recording import finalize_voice, remove_partial

# 상담원이 올린 WAV의 등록·재생 라우트다. AI 녹취와 공통으로 쓰는 재생·보관 기한 처리도 여기 둔다.
recordings = Blueprint("recordings", __name__)


def recording_info(item):
    # 파일 경로와 원본 음성은 목록에 싣지 않는다. 상태와 만료 시각으로 재생 가능 여부를 계산하고,
    # 직원 업로더의 유무로 AI 통화 녹취인지 표시한다. 삭제 대기나 만료 상태에서는 표시명도 감춘다.
    return {
        "recording_id": item["recording_id"], "state": item["state"],
        "expires_at": stamp(item["expires_at"]),
        "playable": item["state"] == "READY" and item["expires_at"] > utcnow(),
        "label": item.get("label") if item["expires_at"] > utcnow() and item["state"] not in ("DELETE_PENDING", "DESTROYED") else None,
        "ai": item["uploaded_by"] is None,
    }


def read_wav():
    # multipart의 file 필드에서 상한보다 한 바이트 더 읽어, 크기를 초과한 파일을 구별한다.
    upload = request.files.get("file")
    if upload is None:
        raise ApiError("INVALID_INPUT")
    limit = current_app.config["WAV_LIMIT"]
    data = upload.stream.read(limit + 1)
    if len(data) > limit:
        raise ApiError("RECORDING_TOO_LARGE")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            # 헤더의 프레임 수 × 채널 수 × 샘플 바이트 수가 실제 음성 데이터와 일치하는지 검사한다.
            # 빈 녹음이나 중간에 잘린 파일은 저장 단계로 넘기지 않는다.
            frames = audio.getnframes()
            size = frames * audio.getnchannels() * audio.getsampwidth()
            if not frames or not audio.getframerate() or size > len(data) or len(audio.readframes(frames)) != size:
                raise ValueError
    except (EOFError, ValueError, wave.Error, struct.error):
        raise ApiError("WAV_FORMAT_UNSUPPORTED") from None
    return data


def can_upload(case):
    # 담당자 확인을 끝낸 티켓에서 호출한다. 종결되지 않은 전화 상담이고 녹취 동의를 받았어야 한다.
    channel(case, "PHONE")
    writable(case)
    consent = one("SELECT consent_state FROM phone_response WHERE case_id=%s", (case["case_id"],))
    if not consent or consent["consent_state"] != "AGREED":
        raise ApiError("RECORDING_CONSENT_REQUIRED")


def check_expiry(item):
    # 파일 정리가 아직 끝나지 않았어도 기한이 지났거나 삭제를 예약한 녹취에는 접근을 허용하지 않는다.
    if item["state"] in ("DESTROYED", "DELETE_PENDING") or item["expires_at"] <= utcnow():
        raise ApiError("RECORDING_EXPIRED")


@recordings.post("/staff/cases/<uuid:case_id>/recordings")
def upload_recording(case_id):
    # 파일을 읽기 전 담당·동의를 확인한다. 검증과 해시 계산 중에는 티켓 잠금을 잡지 않고,
    # 저장을 예약할 때 다시 잠가 그사이의 재배정·종결·동의 변경을 반영한다.
    can_upload(assigned_case(case_id))
    request_id = uuid_field(request.form.get("upload_request_id"))
    data = read_wav()
    checksum = hashlib.sha256(data).digest()
    can_upload(assigned_case(case_id, lock=True))
    item = one("SELECT * FROM recording WHERE case_id=%s AND upload_request_id=%s FOR UPDATE", (case_id, request_id))
    created = item is None
    if item:
        # 같은 티켓의 업로드 요청 ID는 같은 직원과 같은 파일에만 재사용할 수 있다.
        # 이미 READY면 즉시 기존 결과를, 저장이 끝나지 않았으면 기존 녹취 ID를 사용한다.
        check_expiry(item)
        if item["uploaded_by"] != g.actor or item["sha256"] != checksum or item["size_bytes"] != len(data):
            raise ApiError("REQUEST_KEY_CONFLICT")
        if item["state"] == "READY":
            return recording_info(item)
        recording_id = item["recording_id"]
    else:
        recording_id = new_id()
        run(
            "INSERT INTO recording (recording_id, case_id, upload_request_id, uploaded_by, state, object_key, "
            "size_bytes, sha256, expires_at) VALUES (%s,%s,%s,%s,'PENDING',%s,%s,%s,%s)",
            (recording_id, case_id, request_id, g.actor, recording_store.object_key(recording_id), len(data), checksum,
             deadline("RECORDING_RETENTION_MINUTES", 5)),
        )
    # 파일 쓰기 전에 예약을 별도로 커밋한다. 중간에 프로세스가 끝나도 재시도나 정리 작업이
    # PENDING 행을 찾을 수 있다. 커밋으로 잠금이 풀렸으므로 담당·동의·기한을 다시 확인한다.
    db().commit()
    can_upload(assigned_case(case_id, lock=True))
    item = one("SELECT * FROM recording WHERE recording_id=%s FOR UPDATE", (recording_id,))
    check_expiry(item)
    if item["state"] != "READY":
        try:
            recording_store.put_audio(item["object_key"], io.BytesIO(data))
        except OSError:
            # 실패를 먼저 커밋해 요청 종료 시의 롤백 뒤에도 같은 ID로 저장을 다시 시도할 수 있게 한다.
            run("UPDATE recording SET state='FAILED' WHERE recording_id=%s", (recording_id,))
            db().commit()
            raise ApiError("RECORDING_STORE_FAILED") from None
        # 실제 파일 쓰기가 끝난 시점부터 재생 가능한 보관 기간을 계산한다.
        expires = deadline("RECORDING_RETENTION_MINUTES", 5)
        run(
            "UPDATE recording SET state='READY', stored_at=UTC_TIMESTAMP(6), expires_at=%s WHERE recording_id=%s",
            (expires, recording_id),
        )
        item.update(state="READY", expires_at=expires)
    return recording_info(item), 201 if created else 200


@recordings.get("/staff/cases/<uuid:case_id>/recordings/<uuid:recording_id>/audio")
def play(case_id, recording_id):
    # URL의 티켓에 실제로 연결된 녹취만 찾고 현재 담당자인지 확인한다. 재생 시도는 감사 대상이다.
    g.audit_target = ("RECORDING_PLAY", case_id, recording_id)
    assigned_case(case_id, lock=True)
    item = one("SELECT * FROM recording WHERE recording_id=%s AND case_id=%s FOR UPDATE", (recording_id, case_id))
    if not item:
        raise ApiError("RECORDING_NOT_FOUND")
    return stream_recording(item)


def stream_recording(item):
    # 권한을 확인한 호출자가 넘긴 녹취의 기한과 준비 상태를 검사한 뒤 Flask 파일 응답을 만든다.
    # 저장소에서 파일을 열 수 없으면 실패 상태를 확정하고 API 오류로 알린다.
    check_expiry(item)
    if item["state"] != "READY":
        raise ApiError("RECORDING_NOT_READY")
    try:
        return recording_store.send_audio(item["object_key"])
    except OSError:
        run("UPDATE recording SET state='FAILED' WHERE recording_id=%s", (item["recording_id"],))
        db().commit()
        raise ApiError("RECORDING_STORE_FAILED") from None


def purge_case_content(case_id):
    # 채팅·전화 응대·직원 메모·종결 메모를 제거하고 처리 시각을 남긴다.
    # 티켓의 상태와 배정 이력은 유지하며, 녹취 파일은 별도의 정리 절차를 따른다.
    run("DELETE FROM chat_message WHERE case_id=%s", (case_id,))
    run("DELETE FROM phone_response WHERE case_id=%s", (case_id,))
    run("DELETE FROM staff_note WHERE case_id=%s", (case_id,))
    run("UPDATE support_case SET result_note=NULL, content_purged_at=UTC_TIMESTAMP(6) WHERE case_id=%s", (case_id,))


def purge():
    # Flask CLI와 주기적 유지보수 작업이 공유한다. 먼저 정리 후보 ID만 읽고 각 행을 잠근 뒤
    # 현재 상태·기한을 다시 판단한다. 한 건씩 커밋해 처리한 녹취의 잠금을 바로 놓는다.
    due = rows(
        "SELECT recording_id FROM recording WHERE state='DELETE_PENDING' "
        "OR (state<>'DESTROYED' AND expires_at<=UTC_TIMESTAMP(6))"
    )
    for row in due:
        rid = row["recording_id"]
        item = one("SELECT * FROM recording WHERE recording_id=%s FOR UPDATE", (rid,))
        if item["uploaded_by"] is None and item["state"] == "PENDING" and item["expires_at"] <= utcnow() and item["size_bytes"]:
            # 브라우저가 종료 요청을 보내지 못한 경우다. DB가 수신 확인한 음성이 있으면 WAV로 완성한다.
            # 저장 오류는 재시도하되 유휴 만료 후 보관 기간까지 지나면 삭제 대기로 넘긴다.
            try:
                finalize_voice(item)
            except OSError:
                audit("RECORDING_STORE", "retention", item["case_id"], rid, "RETRY")
                if utcnow() - item["expires_at"] >= timedelta(minutes=int(os.getenv("RECORDING_RETENTION_MINUTES", 5))):
                    run("UPDATE recording SET state='DELETE_PENDING' WHERE recording_id=%s", (rid,))
            db().commit()
            continue
        if item["state"] != "DESTROYED" and (item["state"] == "DELETE_PENDING" or item["expires_at"] <= utcnow()):
            # 임시 파일과 완성 파일을 지운 뒤에만 파기 완료로 표시한다.
            # 저장소 오류면 DELETE_PENDING으로 남겨 다음 정리 회차에서 다시 처리한다.
            try:
                if item["uploaded_by"] is None:
                    remove_partial(rid)
                recording_store.delete_audio(item["object_key"])
            except OSError:
                run("UPDATE recording SET state='DELETE_PENDING' WHERE recording_id=%s", (rid,))
                audit("RECORDING_PURGE", "retention", item["case_id"], rid, "RETRY")
            else:
                run(
                    "UPDATE recording SET state='DESTROYED', object_key=NULL, size_bytes=NULL, sha256=NULL, label=NULL, "
                    "destroyed_at=UTC_TIMESTAMP(6) WHERE recording_id=%s", (rid,),
                )
                audit("RECORDING_PURGE", "retention", item["case_id"], rid)
        db().commit()

    # 상담 본문의 기한은 녹취와 독립적이다. 아직 본문을 정리하지 않은 티켓만 처리한다.
    due = rows("SELECT case_id FROM support_case WHERE content_expires_at<=UTC_TIMESTAMP(6) AND content_purged_at IS NULL")
    for row in due:
        key = row["case_id"]
        case = one("SELECT content_purged_at,content_expires_at FROM support_case WHERE case_id=%s FOR UPDATE", (key,))
        if not case["content_purged_at"] and case["content_expires_at"] <= utcnow():
            purge_case_content(key)
            audit("CASE_CONTENT_PURGE", "retention", key)
        db().commit()
    # 마지막으로 만료된 입장 코드·세션과, 원본 녹취가 실패·삭제·만료 상태에 해당하는 요약을 정리한다.
    for table in ("entry_code", "customer_session", "staff_session"):
        run(f"DELETE FROM {table} WHERE expires_at<=UTC_TIMESTAMP(6)")
    run("DELETE s FROM ai_call_summary s JOIN recording r ON r.recording_id=s.recording_id "
        "WHERE r.state IN ('DELETE_PENDING','DESTROYED','FAILED') OR (r.state='READY' AND r.expires_at<=UTC_TIMESTAMP(6))")
    db().commit()
