"""채팅 입장 코드, 고객 세션과 메시지."""

from ..db import one, rows, run


def issue_entry_code(code_hash, case_id, expires_at):
    run(
        "INSERT INTO entry_code (code_hash, case_id, expires_at) VALUES (%s,%s,%s)",
        (code_hash, case_id, expires_at),
    )


def lock_case_for_entry(code_hash):
    return one(
        "SELECT case_id,closed_at FROM visible_case WHERE case_id="
        "(SELECT case_id FROM entry_code WHERE code_hash=%s) FOR UPDATE",
        (code_hash,),
    )


def lock_entry_code(code_hash):
    return one("SELECT * FROM entry_code WHERE code_hash=%s FOR UPDATE", (code_hash,))


def consume_entry_code(code_hash):
    run("UPDATE entry_code SET used_at=UTC_TIMESTAMP(6) WHERE code_hash=%s", (code_hash,))


def create_session(token_hash, case_id, expires_at):
    run(
        "INSERT INTO customer_session (token_hash,case_id,expires_at) VALUES (%s,%s,%s)",
        (token_hash, case_id, expires_at),
    )


def create_visitor_session(token_hash, expires_at):
    run("INSERT INTO customer_session (token_hash,expires_at) VALUES (%s,%s)", (token_hash, expires_at))


def session(token_hash, *, lock=False):
    return one(
        "SELECT case_id, expires_at FROM customer_session "
        "WHERE token_hash=%s AND expires_at>UTC_TIMESTAMP(6)" + (" FOR UPDATE" if lock else ""), (token_hash,),
    )


def link_session(token_hash, case_id, expires_at):
    run(
        "UPDATE customer_session SET case_id=%s,expires_at=%s WHERE token_hash=%s",
        (case_id, expires_at, token_hash),
    )


def delete_session(token_hash):
    run("DELETE FROM customer_session WHERE token_hash=%s", (token_hash,))


def revoke_entry_codes(case_id):
    run("DELETE FROM entry_code WHERE case_id=%s", (case_id,))


def delete_case_sessions(case_id):
    run("DELETE FROM customer_session WHERE case_id=%s", (case_id,))


def delete_expired_sessions():
    run("DELETE FROM entry_code WHERE expires_at<=UTC_TIMESTAMP(6)")
    run("DELETE FROM customer_session WHERE expires_at<=UTC_TIMESTAMP(6)")


def messages(case_id):
    return rows("SELECT * FROM chat_message WHERE case_id=%s AND body<>'' ORDER BY message_id", (case_id,))


def message_by_request(case_id, client_message_id):
    return one("SELECT * FROM chat_message WHERE case_id=%s AND client_message_id=%s", (case_id, client_message_id))


def message(message_id):
    return one("SELECT * FROM chat_message WHERE message_id=%s", (message_id,))


def add_message(case_id, client_message_id, author_id, body):
    return run(
        "INSERT INTO chat_message (case_id, client_message_id, author_staff_id, body) VALUES (%s,%s,%s,%s)",
        (case_id, client_message_id, author_id, body),
    )


def message_for_edit(case_id, message_id):
    return one(
        "SELECT body FROM chat_message WHERE case_id=%s AND message_id=%s AND body<>''",
        (case_id, message_id),
    )


def update_message(case_id, message_id, body):
    run("UPDATE chat_message SET body=%s WHERE case_id=%s AND message_id=%s", (body, case_id, message_id))


def clear_message(case_id, message_id):
    run("UPDATE chat_message SET body='' WHERE case_id=%s AND message_id=%s", (case_id, message_id))


def clear_messages(case_id):
    run("UPDATE chat_message SET body='' WHERE case_id=%s AND body<>''", (case_id,))


def delete_messages(case_id):
    run("DELETE FROM chat_message WHERE case_id=%s", (case_id,))
