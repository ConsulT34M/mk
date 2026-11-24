"""Operations on private recording objects. Callers keep only the relative key."""
import os
from pathlib import Path
from shutil import copyfileobj

from flask import current_app, send_file


def object_key(recording_id):
    return f"files/{recording_id}.wav"


def _path(key):
    return Path(current_app.config["RECORDING_DIR"]) / key


def put_audio(key, source):
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
    return send_file(_path(key), mimetype="audio/wav", download_name="recording.wav", etag=False)


def delete_audio(key):
    path = _path(key)
    path.with_suffix(".tmp").unlink(missing_ok=True)
    path.unlink(missing_ok=True)
