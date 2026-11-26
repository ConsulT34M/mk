"""Receive consented mono PCM in order, then publish one WAV per conversation."""
import hashlib
import os
import struct
from datetime import timedelta
from pathlib import Path

from flask import current_app

from . import recording_store
from .common import ApiError, deadline
from .db import db, utcnow
from .queries import recordings as recording_queries

# 브라우저의 녹음 형식은 16 kHz, 16비트, 모노 PCM이다. 1초분은 32,000바이트다.
# 앞의 44바이트를 WAV 헤더 자리에 남겨 두고 최종 길이가 정해지면 그 자리를 채운다.
SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2
HEADER_BYTES = 44


def idle_deadline():
    # 브라우저는 무음도 포함해 통상 1초 분량씩 보낸다. 새 데이터가 들어올 때마다
    # 유휴 기한을 2분 뒤로 연장해, 종료 알림을 놓친 녹취도 정리 작업에서 찾을 수 있게 한다.
    return utcnow() + timedelta(minutes=2)


def reserve_voice(voice_id):
    # 통화 ID를 녹취 ID와 요청 ID로 함께 써서 인계·요약·종료가 같은 행을 찾게 한다.
    # PENDING 동안 size_bytes는 저장이 확인된 PCM 본문의 길이이며 처음에는 0이다.
    recording_queries.reserve_voice_recording(
        voice_id, recording_store.object_key(voice_id), current_app.config["S3_RECORDING_BUCKET"], idle_deadline(),
    )


def partial_path(voice_id):
    # 수신 중인 음성은 저장소의 .part 파일에 모은다. 확정된 WAV와 경로를 구분한다.
    return Path(current_app.config["RECORDING_DIR"]) / (voice_id + ".part")


def remove_partial(voice_id):
    # 빈 통화 종료, 정상 저장, 만료 정리에서 공통으로 호출하므로 이미 지운 파일도 허용한다.
    partial_path(voice_id).unlink(missing_ok=True)


def append_voice(item, offset, data):
    # 호출자가 녹취 행을 잠근 상태에서 처리한다. 샘플 하나가 2바이트이므로
    # 시작 위치와 본문 길이가 모두 짝수여야 하며 빈 본문은 받지 않는다.
    if item["state"] != "PENDING":
        raise ApiError("VOICE_RECORDING_ENDED")
    if not data or len(data) % 2 or offset % 2:
        raise ApiError("INVALID_INPUT")
    total = item["size_bytes"]
    end = offset + len(data)
    # 통화 길이의 상한을 같은 PCM 형식의 바이트 수로 환산하고 이번 조각의 끝 위치와 비교한다.
    if end > int(os.environ.get("CHAT_SESSION_MINUTES", 60)) * 60 * BYTES_PER_SECOND:
        raise ApiError("RECORDING_TOO_LARGE")
    # 이미 수신한 범위 안에 완전히 들어가는 재전송, 또는 정확히 끝에 이어지는 새 조각만 허용한다.
    # 중간을 건너뛰거나 기존 구간과 새 구간을 걸치는 요청은 어느 부분을 인정할지 모호하다.
    if offset > total or (offset < total < end):
        raise ApiError("REQUEST_KEY_CONFLICT")
    path = partial_path(item["recording_id"])
    if not total:
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("r+b" if total else "w+b") as audio:
        if total and os.fstat(audio.fileno()).st_size < HEADER_BYTES + total:
            raise OSError("Incomplete voice recording")
        audio.seek(HEADER_BYTES + offset)
        if offset < total:
            # 응답을 받지 못해 같은 위치로 재시도한 경우 기존 바이트가 같을 때만 성공으로 돌려준다.
            if audio.read(len(data)) != data:
                raise ApiError("REQUEST_KEY_CONFLICT")
            return
        audio.write(data)
        audio.flush()
        os.fsync(audio.fileno())
    # 파일 쓰기를 디스크에 반영한 후에만 DB의 수신 길이를 늘린다. 이후 요청은 이 길이를 기준으로 삼는다.
    recording_queries.record_progress(item["recording_id"], end, idle_deadline())


def finalize_voice(item):
    """Caller holds the recording row lock. Only committed samples become playable."""
    # 이미 처리를 마친 행은 READY 여부만 반환한다. 수신 데이터가 없는 통화는
    # 빈 WAV를 만들지 않고 파기 상태로 마감한다.
    if item["state"] != "PENDING":
        return item["state"] == "READY"
    rid = item["recording_id"]
    length = item["size_bytes"]
    if not length:
        remove_partial(rid)
        recording_queries.discard_empty_recording(rid)
        return False
    # DB에서 확인한 PCM 길이를 RIFF 전체 크기 필드와 data 길이 필드에 반영해 헤더를 만든다.
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + length, b"WAVE", b"fmt ", 16, 1, 1,
                         SAMPLE_RATE, BYTES_PER_SECOND, 2, 16, b"data", length)
    with partial_path(rid).open("r+b") as audio:
        if os.fstat(audio.fileno()).st_size < HEADER_BYTES + length:
            raise OSError("Incomplete voice recording")
        audio.write(header)
        # 파일 쓰기 직후 DB 갱신이 중단돼 끝에 미확정 데이터가 남았을 수 있다.
        # 확인된 길이까지만 남기고, 헤더를 포함한 최종 파일의 해시와 크기를 기록한다.
        audio.truncate(HEADER_BYTES + length)
        audio.seek(0)
        checksum = hashlib.file_digest(audio, "sha256").digest()
        audio.seek(0)
        recording_store.put_audio(item, audio)
    recording_queries.finish_voice_recording(
        rid, HEADER_BYTES + length, checksum, deadline("RECORDING_RETENTION_MINUTES", 5),
    )
    # 완성 파일과 READY 상태를 확정한 후 임시본을 지운다. DB 확정 전에는 원본을 이용해 재시도할 수 있다.
    db().commit()
    try:
        remove_partial(rid)
    except OSError:
        pass  # 남은 임시본은 보관 기한이 끝날 때 정리 작업이 다시 삭제한다.
    return True
