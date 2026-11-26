"""Read and write each recording at the location reserved for it in MySQL."""
import os
from contextlib import closing, contextmanager
from functools import cache
from pathlib import Path
from shutil import copyfileobj

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import current_app, request, send_file
from werkzeug.exceptions import RequestedRangeNotSatisfiable


def object_key(recording_id):
    return f"files/{recording_id}.wav"


def _path(key):
    # 요청 처리와 유지보수 CLI 모두 현재 앱의 RECORDING_DIR을 기준으로 동일한 파일을 찾는다.
    return Path(current_app.config["RECORDING_DIR"]) / key


@cache
def _s3_client():
    return boto3.session.Session().client("s3", region_name=os.getenv("AWS_REGION") or None)


@contextmanager
def _s3():
    try:
        yield _s3_client()
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code == "NoSuchKey":
            raise FileNotFoundError("S3 recording missing") from error
        raise OSError(f"S3 request failed ({code})") from error
    except BotoCoreError as error:
        raise OSError("S3 connection or configuration error") from error


@contextmanager
def open_audio(item):
    key = item["object_key"]
    if item["storage_bucket"]:
        with _s3() as client:
            result = client.get_object(Bucket=item["storage_bucket"], Key=key)
            with closing(result["Body"]) as audio:
                yield audio
    else:
        with _path(key).open("rb") as audio:
            yield audio


def put_audio(item, source):
    key = item["object_key"]
    if item["storage_bucket"]:
        with _s3() as client:
            client.put_object(Bucket=item["storage_bucket"], Key=key, Body=source, ContentType="audio/wav")
        return
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


def send_audio(item):
    # 접근 권한과 만료 여부는 호출자가 확인한다. 브라우저에는 공통 파일명과 WAV 미디어 타입을 전달한다.
    key = item["object_key"]
    if not item["storage_bucket"]:
        return send_file(_path(key), mimetype="audio/wav", download_name="recording.wav", etag=False)

    params = {"Bucket": item["storage_bucket"], "Key": key}
    if "Range" in request.headers and "If-Range" not in request.headers:
        requested = request.range
        span = requested.range_for_length(item["size_bytes"]) if requested else None
        if span is None:
            raise RequestedRangeNotSatisfiable(item["size_bytes"])
        params["Range"] = f"bytes={span[0]}-{span[1] - 1}"
    with _s3() as client:
        result = client.get_object(**params)
    body = result["Body"]
    response = current_app.response_class(body.iter_chunks(64 * 1024), mimetype="audio/wav")
    response.call_on_close(body.close)
    response.content_length = result["ContentLength"]
    response.headers["Content-Disposition"] = "inline; filename=recording.wav"
    response.headers["Accept-Ranges"] = "bytes"
    if "ContentRange" in result:
        response.status_code = 206
        response.headers["Content-Range"] = result["ContentRange"]
    return response


def delete_audio(item):
    key = item["object_key"]
    if item["storage_bucket"]:
        with _s3() as client:
            client.delete_object(Bucket=item["storage_bucket"], Key=key)
        return
    # 중단된 쓰기의 임시 파일도 함께 정리한다. 이전 회차에서 이미 제거한 파일은 건너뛴다.
    path = _path(key)
    path.with_suffix(".tmp").unlink(missing_ok=True)
    path.unlink(missing_ok=True)
