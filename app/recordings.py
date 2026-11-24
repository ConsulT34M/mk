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

recordings = Blueprint("recordings", __name__)


def recording_info(item):
    return {
        "recording_id": item["recording_id"], "state": item["state"],
        "expires_at": stamp(item["expires_at"]),
        "playable": item["state"] == "READY" and item["expires_at"] > utcnow(),
        "label": item.get("label") if item["expires_at"] > utcnow() and item["state"] not in ("DELETE_PENDING", "DESTROYED") else None,
        "ai": item["uploaded_by"] is None,
    }


def read_wav():
    upload = request.files.get("file")
    if upload is None:
        raise ApiError("INVALID_INPUT")
    limit = current_app.config["WAV_LIMIT"]
    data = upload.stream.read(limit + 1)
    if len(data) > limit:
        raise ApiError("RECORDING_TOO_LARGE")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            frames = audio.getnframes()
            size = frames * audio.getnchannels() * audio.getsampwidth()
            if not frames or not audio.getframerate() or size > len(data) or len(audio.readframes(frames)) != size:
                raise ValueError
    except (EOFError, ValueError, wave.Error, struct.error):
        raise ApiError("WAV_FORMAT_UNSUPPORTED") from None
    return data


def can_upload(case):
    channel(case, "PHONE")
    writable(case)
    consent = one("SELECT consent_state FROM phone_response WHERE case_id=%s", (case["case_id"],))
    if not consent or consent["consent_state"] != "AGREED":
        raise ApiError("RECORDING_CONSENT_REQUIRED")


def check_expiry(item):
    if item["state"] in ("DESTROYED", "DELETE_PENDING") or item["expires_at"] <= utcnow():
        raise ApiError("RECORDING_EXPIRED")


@recordings.post("/staff/cases/<uuid:case_id>/recordings")
def upload_recording(case_id):
    can_upload(assigned_case(case_id))
    request_id = uuid_field(request.form.get("upload_request_id"))
    data = read_wav()
    checksum = hashlib.sha256(data).digest()
    can_upload(assigned_case(case_id, lock=True))
    item = one("SELECT * FROM recording WHERE case_id=%s AND upload_request_id=%s FOR UPDATE", (case_id, request_id))
    created = item is None
    if item:
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
    # A crash during I/O leaves a durable, non-playable reservation for retry or cleanup.
    db().commit()
    can_upload(assigned_case(case_id, lock=True))
    item = one("SELECT * FROM recording WHERE recording_id=%s FOR UPDATE", (recording_id,))
    check_expiry(item)
    if item["state"] != "READY":
        try:
            recording_store.put_audio(item["object_key"], io.BytesIO(data))
        except OSError:
            run("UPDATE recording SET state='FAILED' WHERE recording_id=%s", (recording_id,))
            db().commit()
            raise ApiError("RECORDING_STORE_FAILED") from None
        expires = deadline("RECORDING_RETENTION_MINUTES", 5)
        run(
            "UPDATE recording SET state='READY', stored_at=UTC_TIMESTAMP(6), expires_at=%s WHERE recording_id=%s",
            (expires, recording_id),
        )
        item.update(state="READY", expires_at=expires)
    return recording_info(item), 201 if created else 200


@recordings.get("/staff/cases/<uuid:case_id>/recordings/<uuid:recording_id>/audio")
def play(case_id, recording_id):
    g.audit_target = ("RECORDING_PLAY", case_id, recording_id)
    assigned_case(case_id, lock=True)
    item = one("SELECT * FROM recording WHERE recording_id=%s AND case_id=%s FOR UPDATE", (recording_id, case_id))
    if not item:
        raise ApiError("RECORDING_NOT_FOUND")
    return stream_recording(item)


def stream_recording(item):
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
    run("DELETE FROM chat_message WHERE case_id=%s", (case_id,))
    run("DELETE FROM phone_response WHERE case_id=%s", (case_id,))
    run("DELETE FROM staff_note WHERE case_id=%s", (case_id,))
    run("UPDATE support_case SET result_note=NULL, content_purged_at=UTC_TIMESTAMP(6) WHERE case_id=%s", (case_id,))


def purge():
    due = rows(
        "SELECT recording_id FROM recording WHERE state='DELETE_PENDING' "
        "OR (state<>'DESTROYED' AND expires_at<=UTC_TIMESTAMP(6))"
    )
    for row in due:
        rid = row["recording_id"]
        item = one("SELECT * FROM recording WHERE recording_id=%s FOR UPDATE", (rid,))
        if item["uploaded_by"] is None and item["state"] == "PENDING" and item["expires_at"] <= utcnow() and item["size_bytes"]:
            # A closed browser may not send the final request. Keep its acknowledged audio.
            try:
                finalize_voice(item)
            except OSError:
                audit("RECORDING_STORE", "retention", item["case_id"], rid, "RETRY")
                if utcnow() - item["expires_at"] >= timedelta(minutes=int(os.getenv("RECORDING_RETENTION_MINUTES", 5))):
                    run("UPDATE recording SET state='DELETE_PENDING' WHERE recording_id=%s", (rid,))
            db().commit()
            continue
        if item["state"] != "DESTROYED" and (item["state"] == "DELETE_PENDING" or item["expires_at"] <= utcnow()):
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

    due = rows("SELECT case_id FROM support_case WHERE content_expires_at<=UTC_TIMESTAMP(6) AND content_purged_at IS NULL")
    for row in due:
        key = row["case_id"]
        case = one("SELECT content_purged_at,content_expires_at FROM support_case WHERE case_id=%s FOR UPDATE", (key,))
        if not case["content_purged_at"] and case["content_expires_at"] <= utcnow():
            purge_case_content(key)
            audit("CASE_CONTENT_PURGE", "retention", key)
        db().commit()
    for table in ("entry_code", "customer_session", "staff_session"):
        run(f"DELETE FROM {table} WHERE expires_at<=UTC_TIMESTAMP(6)")
    db().commit()
