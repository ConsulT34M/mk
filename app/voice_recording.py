"""Receive consented mono PCM in order, then publish one WAV per conversation."""
import hashlib
import os
import struct
from datetime import timedelta
from pathlib import Path

from flask import current_app

from . import recording_store
from .common import ApiError, deadline
from .db import db, run, utcnow

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2
HEADER_BYTES = 44


def idle_deadline():
    # The browser sends sound (including silence) once per second.
    return utcnow() + timedelta(minutes=2)


def reserve_voice(voice_id):
    run("INSERT INTO recording (recording_id,upload_request_id,state,object_key,size_bytes,expires_at) "
        "VALUES (%s,%s,'PENDING',%s,0,%s)",
        (voice_id, voice_id, recording_store.object_key(voice_id), idle_deadline()))


def partial_path(voice_id):
    return Path(current_app.config["RECORDING_DIR"]) / (voice_id + ".part")


def remove_partial(voice_id):
    partial_path(voice_id).unlink(missing_ok=True)


def append_voice(item, offset, data):
    if item["state"] != "PENDING":
        raise ApiError("VOICE_RECORDING_ENDED")
    if not data or len(data) % 2 or offset % 2:
        raise ApiError("INVALID_INPUT")
    total = item["size_bytes"]
    end = offset + len(data)
    if end > int(os.environ.get("CHAT_SESSION_MINUTES", 60)) * 60 * BYTES_PER_SECOND:
        raise ApiError("RECORDING_TOO_LARGE")
    if offset > total or (offset < total < end):
        raise ApiError("REQUEST_KEY_CONFLICT")
    path = partial_path(item["recording_id"])
    with path.open("r+b" if total else "w+b") as audio:
        if total and os.fstat(audio.fileno()).st_size < HEADER_BYTES + total:
            raise OSError("Incomplete voice recording")
        audio.seek(HEADER_BYTES + offset)
        if offset < total:
            if audio.read(len(data)) != data:
                raise ApiError("REQUEST_KEY_CONFLICT")
            return
        audio.write(data)
        audio.flush()
        os.fsync(audio.fileno())
    run("UPDATE recording SET size_bytes=%s,expires_at=%s WHERE recording_id=%s",
        (end, idle_deadline(), item["recording_id"]))


def finalize_voice(item):
    """Caller holds the recording row lock. Only committed samples become playable."""
    if item["state"] != "PENDING":
        return item["state"] == "READY"
    rid = item["recording_id"]
    length = item["size_bytes"]
    if not length:
        remove_partial(rid)
        run("UPDATE recording SET state='DESTROYED',object_key=NULL,size_bytes=NULL,label=NULL,destroyed_at=UTC_TIMESTAMP(6) "
            "WHERE recording_id=%s", (rid,))
        return False
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + length, b"WAVE", b"fmt ", 16, 1, 1,
                         SAMPLE_RATE, BYTES_PER_SECOND, 2, 16, b"data", length)
    with partial_path(rid).open("r+b") as audio:
        if os.fstat(audio.fileno()).st_size < HEADER_BYTES + length:
            raise OSError("Incomplete voice recording")
        audio.write(header)
        audio.truncate(HEADER_BYTES + length)
        audio.seek(0)
        checksum = hashlib.file_digest(audio, "sha256").digest()
        audio.seek(0)
        recording_store.put_audio(item["object_key"], audio)
    run("UPDATE recording SET state='READY',size_bytes=%s,sha256=%s,stored_at=UTC_TIMESTAMP(6),expires_at=%s "
        "WHERE recording_id=%s", (HEADER_BYTES + length, checksum, deadline("RECORDING_RETENTION_MINUTES", 5), rid))
    # Keep the recoverable partial until the finished object's DB state is durable.
    db().commit()
    try:
        remove_partial(rid)
    except OSError:
        pass  # The retention worker retries at expiry.
    return True
