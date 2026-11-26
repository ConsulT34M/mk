"""RKX AI 채팅 설정과 대화 내역."""

from ..db import one, rows, run


def policy():
    return one("SELECT enabled,greeting,reply_style FROM ai_chat_policy WHERE policy_id=1")


def update_policy(enabled, greeting, style):
    run("UPDATE ai_chat_policy SET enabled=%s,greeting=%s,reply_style=%s WHERE policy_id=1", (enabled, greeting, style))


def latest(subject):
    return one("SELECT * FROM ai_chat_session WHERE subject_ref=%s ORDER BY updated_at DESC,chat_id DESC LIMIT 1", (subject,))


def session(chat_id):
    return one("SELECT * FROM ai_chat_session WHERE chat_id=%s", (chat_id,))


def lock_session(chat_id, subject, greeting):
    run("INSERT INTO ai_chat_session (chat_id,subject_ref,greeting) VALUES (%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE chat_id=ai_chat_session.chat_id", (chat_id, subject, greeting))
    return owned_for_update(chat_id, subject)


def owned_for_update(chat_id, subject):
    return one("SELECT * FROM ai_chat_session WHERE chat_id=%s AND subject_ref=%s FOR UPDATE", (chat_id, subject))


def prior_turn(chat_id, request_id):
    return one("SELECT question FROM ai_chat_turn WHERE chat_id=%s AND request_id=%s", (chat_id, request_id))


def turns(chat_id, limit=100, offset=0):
    return rows("SELECT question,answer,created_at FROM ai_chat_turn WHERE chat_id=%s "
                "ORDER BY turn_id DESC LIMIT %s OFFSET %s", (chat_id, limit, offset))


def add_turn(chat_id, request_id, question, answer):
    run("INSERT INTO ai_chat_turn (chat_id,request_id,question,answer) VALUES (%s,%s,%s,%s)",
        (chat_id, request_id, question, answer))
    run("UPDATE ai_chat_session SET updated_at=UTC_TIMESTAMP(6) WHERE chat_id=%s", (chat_id,))


def sessions(limit, offset):
    return rows("SELECT s.*, (SELECT question FROM ai_chat_turn t WHERE t.chat_id=s.chat_id ORDER BY turn_id LIMIT 1) AS subject "
                "FROM ai_chat_session s ORDER BY s.updated_at DESC,s.chat_id DESC LIMIT %s OFFSET %s", (limit, offset))
