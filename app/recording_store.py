"""Operations on private recording objects. Callers keep only the relative key."""
import os
from pathlib import Path
from shutil import copyfileobj

from flask import current_app, send_file


def object_key(recording_id):
    # DB에는 녹취 ID로 만든 상대 키를 저장한다. 실제 저장 디렉터리는 앱 설정에서 결정한다.
    return f"files/{recording_id}.wav"


def _path(key):
    # 요청 처리와 유지보수 CLI 모두 현재 앱의 RECORDING_DIR을 기준으로 동일한 파일을 찾는다.
    return Path(current_app.config["RECORDING_DIR"]) / key


def open_audio(key):
    # 요약 작업은 바이너리 스트림을 받아 WAV를 구간별로 읽고 with 블록에서 닫는다.
    return _path(key).open("rb")


def put_audio(key, source):
    # 업로드 스트림 또는 완성된 AI 녹음을 같은 디렉터리의 임시 파일에 먼저 복사한다.
    # 쓰기를 디스크에 반영한 뒤 최종 이름으로 교체해 재생 경로에 부분 파일이 노출되지 않게 한다.
    path = _path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("wb") as out:
            copyfileobj(source, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def send_audio(key):
    # 접근 권한과 만료 여부는 호출자가 확인한다. 브라우저에는 공통 파일명과 WAV 미디어 타입을 전달한다.
    return send_file(_path(key), mimetype="audio/wav", download_name="recording.wav", etag=False)


def delete_audio(key):
    # 중단된 쓰기의 임시 파일도 함께 정리한다. 이전 회차에서 이미 제거한 파일은 건너뛴다.
    path = _path(key)
    path.with_suffix(".tmp").unlink(missing_ok=True)
    path.unlink(missing_ok=True)
