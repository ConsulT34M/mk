"""OpenAI 키와 모델 설정."""

from ..db import one, run


def settings(lock=False):
    return one("SELECT key_ciphertext,key_suffix,chat_model,voice_model,summary_model,revision "
               "FROM openai_settings WHERE settings_id=1" + (" FOR UPDATE" if lock else ""))


def save(ciphertext, suffix, chat_model, voice_model, summary_model):
    run("UPDATE openai_settings SET key_ciphertext=COALESCE(%s,key_ciphertext),key_suffix=COALESCE(%s,key_suffix),"
        "chat_model=%s,voice_model=%s,summary_model=%s,revision=revision+1 WHERE settings_id=1",
        (ciphertext, suffix, chat_model, voice_model, summary_model))
