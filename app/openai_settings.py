import json
import os
import re
from http.client import HTTPException
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from .common import ApiError
from .queries import openai


MODELS = {
    "chat_model": ("OPENAI_CHAT_MODEL", ("gpt-4.1-mini",)),
    "voice_model": ("OPENAI_REALTIME_MODEL", ("gpt-realtime-2.1-mini", "gpt-realtime-2.1")),
    "summary_model": ("OPENAI_CALL_SUMMARY_MODEL", ("gpt-audio-1.5", "gpt-audio-mini")),
}


def default_model(field):
    env, choices = MODELS[field]
    return os.environ.get(env, "").strip() or choices[0]


def server_key():
    return os.environ.get("OPENAI_API_KEY", "").strip()


def cipher(*, create=False):
    # DB에는 암호문만 두고 복호화 키는 서비스의 비공개 영구 볼륨에 보관한다.
    path = Path(current_app.config["RECORDING_DIR"]) / ".openai-settings.key"
    try:
        secret = path.read_bytes()
    except FileNotFoundError:
        if not create:
            raise
        # 저장 요청은 DB 설정 행을 잠근 상태다. 완성된 파일만 다른 작업자에 공개한다.
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        secret = Fernet.generate_key()
        temporary = NamedTemporaryFile(dir=path.parent, delete=False)
        try:
            with temporary:
                temporary.write(secret)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary.name, path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)
    return Fernet(secret)


def configured():
    return bool(openai.settings()["key_ciphertext"] or server_key())


def active_key(row):
    if not row["key_ciphertext"]:
        return server_key()
    try:
        return cipher().decrypt(row["key_ciphertext"]).decode("ascii")
    except (OSError, InvalidToken, ValueError):
        raise ApiError("OPENAI_SETTINGS_UNAVAILABLE") from None


def connection(field):
    """한 번의 답변, 통화 또는 녹취 요약에 같은 키와 모델을 사용한다."""
    row = openai.settings()
    return active_key(row), row[field] or default_model(field)


def admin_settings():
    row = openai.settings()
    fallback = server_key()
    models = {}
    for field, (_, choices) in MODELS.items():
        saved = row[field]
        if field == "chat_model":
            choices = ()
        models[field] = {"selected": saved, "default": default_model(field),
                         "choices": (*choices, saved) if saved and saved not in choices else choices}
    return {"models": models, "revision": row["revision"], "stored": bool(row["key_ciphertext"]),
            "configured": bool(row["key_ciphertext"] or fallback),
            "suffix": row["key_suffix"] or (fallback[-4:] if len(fallback) > 4 else "")}


def entered_key(data):
    key = data.get("api_key", "")
    if not isinstance(key, str):
        raise ApiError("INVALID_INPUT")
    key = key.strip()
    if key and not re.fullmatch(r"sk-[A-Za-z0-9_.-]{12,509}", key):
        raise ApiError("OPENAI_INVALID_KEY")
    return key


def is_text_chat_model(name):
    # /models에는 지원 엔드포인트가 없어 일반 텍스트 채팅 계열과 그 파인튜닝 모델을 고른다.
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", name):
        return False
    base = name.split(":", 2)[1] if name.startswith("ft:") else name
    family = re.fullmatch(r"(?:(?:chat)?gpt-\d[\w.-]*|o\d+(?:-[\w.-]+)?)", base, re.ASCII)
    specialized = {"audio", "codex", "cyber", "image", "instruct", "pro", "realtime", "research", "search", "transcribe", "tts"}
    return family is not None and specialized.isdisjoint(base.split("-"))


def model_request(key, model=None):
    if not key:
        raise ApiError("OPENAI_MODEL_KEY_REQUIRED")
    url = "https://api.openai.com/v1/models"
    if model:
        url += "/" + quote(model, safe="")
    req = Request(url, headers={"Accept": "application/json"})
    req.add_unredirected_header("Authorization", "Bearer " + key)
    try:
        with urlopen(req, timeout=8) as response:
            result = json.load(response)
        if isinstance(result, dict):
            return result
    except HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise ApiError("OPENAI_MODEL_AUTH_FAILED") from None
        if error.code == 429:
            raise ApiError("OPENAI_MODEL_LIMIT") from None
        if model and error.code == 404:
            raise ApiError("OPENAI_INVALID_MODEL") from None
    except (URLError, OSError, HTTPException, ValueError):
        pass
    raise ApiError("OPENAI_MODELS_UNAVAILABLE")


def chat_models(data):
    key = entered_key(data) or active_key(openai.settings())
    entries = model_request(key).get("data")
    if not isinstance(entries, list):
        raise ApiError("OPENAI_MODELS_UNAVAILABLE")
    return sorted({item["id"] for item in entries if isinstance(item, dict) and is_text_chat_model(item.get("id"))})


def save_settings(data):
    key = entered_key(data)
    row = openai.settings()
    if str(data.get("revision")) != str(row["revision"]):
        raise ApiError("OPENAI_SETTINGS_CHANGED")
    models = {}
    for field, (_, choices) in MODELS.items():
        selected = data.get(field)
        if not isinstance(selected, str):
            raise ApiError("OPENAI_INVALID_MODEL")
        if selected and selected != row[field]:
            valid = is_text_chat_model(selected) if field == "chat_model" else selected in choices
            if not valid:
                raise ApiError("OPENAI_INVALID_MODEL")
        models[field] = selected or None
    chat = models["chat_model"]
    if chat and (key or chat != row["chat_model"]):
        if model_request(key or active_key(row), chat).get("id") != chat:
            raise ApiError("OPENAI_INVALID_MODEL")
    # 외부 확인 중 다른 관리자가 키나 모델을 바꿨으면 그 변경을 보존한다.
    if openai.settings(lock=True)["revision"] != row["revision"]:
        raise ApiError("OPENAI_SETTINGS_CHANGED")
    ciphertext = None
    if key:
        try:
            ciphertext = cipher(create=True).encrypt(key.encode("ascii"))
        except (OSError, ValueError):
            raise ApiError("OPENAI_SETTINGS_UNAVAILABLE") from None
    openai.save(ciphertext, key[-4:] or None, **models)
