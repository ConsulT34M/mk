"""AI 상담 결과와 상담 시작일 기준 집계."""

from ..db import one, rows, run


def get(channel, session_id):
    return one("SELECT outcome,case_id FROM ai_consultation_outcome WHERE channel=%s AND session_id=%s",
               (channel, session_id))


def add(channel, session_id, outcome, case_id=None):
    run("INSERT INTO ai_consultation_outcome (channel,session_id,outcome,case_id) VALUES (%s,%s,%s,%s)",
        (channel, session_id, outcome, case_id))


def counts(start, end, channel):
    result = []
    if channel != "PHONE":
        # 여러 날 이어진 채팅도 첫 답변이 저장된 날짜에 한 번만 센다.
        result.extend(rows(
            "SELECT 'WEB' AS channel,COALESCE(o.outcome,'UNKNOWN') AS status,COUNT(*) AS n "
            "FROM ai_chat_turn t LEFT JOIN ai_consultation_outcome o ON o.channel='WEB' AND o.session_id=t.chat_id "
            "WHERE t.created_at >= %s AND t.created_at < %s AND t.turn_id=("
            "SELECT first_turn.turn_id FROM ai_chat_turn first_turn WHERE first_turn.chat_id=t.chat_id "
            "ORDER BY first_turn.turn_id LIMIT 1) GROUP BY status", (start, end)))
    if channel != "WEB":
        # 이전 음성 인계도 통화 ID로 발급한 실제 전화 티켓에서 확인할 수 있다.
        result.extend(rows(
            "SELECT 'PHONE' AS channel,CASE WHEN c.case_id IS NOT NULL THEN 'HANDOFF' "
            "ELSE COALESCE(o.outcome,'UNKNOWN') END AS status,COUNT(DISTINCT a.recording_id) AS n "
            "FROM audit_event a LEFT JOIN support_case c ON c.intake_request_id=a.recording_id AND c.channel='PHONE' "
            "LEFT JOIN ai_consultation_outcome o ON o.channel='PHONE' AND o.session_id=a.recording_id "
            "WHERE a.action IN ('VOICE_RECORDING_AGREED','VOICE_RECORDING_REFUSED') AND a.result='OK' "
            "AND a.created_at >= %s AND a.created_at < %s GROUP BY status", (start, end)))
    return result
