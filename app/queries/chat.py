"""채팅 입장 코드, 고객 세션과 메시지."""

from ..db import one, rows, run


def issue_entry_code(code_hash, case_id, expires_at):
    # 상담에 쓸 일회용 입장 코드의 해시와 만료 시각을 저장한다.
    run(
        "INSERT INTO entry_code (code_hash, case_id, expires_at) VALUES (%s,%s,%s)",
        (code_hash, case_id, expires_at),
    )


def lock_case_for_entry(code_hash):
    # 입장 코드가 가리키는 상담을 먼저 잠근다.
    return one(
        "SELECT case_id,closed_at FROM visible_case WHERE case_id="
        "(SELECT case_id FROM entry_code WHERE code_hash=%s) FOR UPDATE",
        (code_hash,),
    )


def lock_entry_code(code_hash):
    # 같은 코드를 동시에 쓰지 못하도록 코드 행을 잠근다.
    return one("SELECT * FROM entry_code WHERE code_hash=%s FOR UPDATE", (code_hash,))


def consume_entry_code(code_hash):
    # 입장 코드의 사용 시각을 기록한다.
    run("UPDATE entry_code SET used_at=UTC_TIMESTAMP(6) WHERE code_hash=%s", (code_hash,))


def create_session(token_hash, case_id, expires_at):
    # 상담과 연결된 고객 채팅 세션을 만든다.
    run(
        "INSERT INTO customer_session (token_hash,case_id,expires_at) VALUES (%s,%s,%s)",
        (token_hash, case_id, expires_at),
    )


def create_visitor_session(token_hash, expires_at):
    # 상담 접수 전 방문자 세션을 만든다.
    run("INSERT INTO customer_session (token_hash,expires_at) VALUES (%s,%s)", (token_hash, expires_at))


def session(token_hash, *, lock=False):
    # 만료 전인 고객 세션을 조회하고 필요하면 잠근다.
    return one(
        "SELECT case_id, expires_at FROM customer_session "
        "WHERE token_hash=%s AND expires_at>UTC_TIMESTAMP(6)" + (" FOR UPDATE" if lock else ""), (token_hash,),
    )


def link_session(token_hash, case_id, expires_at):
    # 방문자 세션에 상담을 연결하고 만료 시각을 갱신한다.
    run(
        "UPDATE customer_session SET case_id=%s,expires_at=%s WHERE token_hash=%s",
        (case_id, expires_at, token_hash),
    )


def delete_session(token_hash):
    # 고객 세션 한 건을 삭제한다.
    run("DELETE FROM customer_session WHERE token_hash=%s", (token_hash,))


def revoke_entry_codes(case_id):
    # 해당 상담의 입장 코드를 모두 폐기한다.
    run("DELETE FROM entry_code WHERE case_id=%s", (case_id,))


def delete_case_sessions(case_id):
    # 해당 상담의 고객 세션을 모두 삭제한다.
    run("DELETE FROM customer_session WHERE case_id=%s", (case_id,))


def delete_expired_sessions():
    # 만료된 입장 코드와 고객 세션을 삭제한다.
    run("DELETE FROM entry_code WHERE expires_at<=UTC_TIMESTAMP(6)")
    run("DELETE FROM customer_session WHERE expires_at<=UTC_TIMESTAMP(6)")


def messages(case_id):
    # 본문이 남은 메시지를 전송 순서대로 조회한다.
    return rows("SELECT * FROM chat_message WHERE case_id=%s AND body<>'' ORDER BY message_id", (case_id,))


def message_by_request(case_id, client_message_id):
    # 재전송 요청에 해당하는 기존 메시지를 찾는다.
    return one("SELECT * FROM chat_message WHERE case_id=%s AND client_message_id=%s", (case_id, client_message_id))


def message(message_id):
    # 메시지 ID로 한 건을 조회한다.
    return one("SELECT * FROM chat_message WHERE message_id=%s", (message_id,))


def add_message(case_id, client_message_id, author_id, body):
    # 메시지를 저장하고 메시지 ID를 반환한다.
    return run(
        "INSERT INTO chat_message (case_id, client_message_id, author_staff_id, body) VALUES (%s,%s,%s,%s)",
        (case_id, client_message_id, author_id, body),
    )


def message_for_edit(case_id, message_id):
    # 상담에서 수정할 메시지의 본문을 조회한다.
    return one(
        "SELECT body FROM chat_message WHERE case_id=%s AND message_id=%s AND body<>''",
        (case_id, message_id),
    )


def update_message(case_id, message_id, body):
    # 메시지 본문을 수정한다.
    run("UPDATE chat_message SET body=%s WHERE case_id=%s AND message_id=%s", (body, case_id, message_id))


def clear_message(case_id, message_id):
    # 메시지 한 건의 본문을 비운다.
    run("UPDATE chat_message SET body='' WHERE case_id=%s AND message_id=%s", (case_id, message_id))


def clear_messages(case_id):
    # 상담 메시지 전체의 본문을 비운다.
    run("UPDATE chat_message SET body='' WHERE case_id=%s AND body<>''", (case_id,))


def delete_messages(case_id):
    # 해당 상담의 메시지 기록을 모두 삭제한다.
    run("DELETE FROM chat_message WHERE case_id=%s", (case_id,))
