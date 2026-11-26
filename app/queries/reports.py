"""상담·통화 통계, 목표 실적과 감사 이력."""

from ..db import one, rows, run

PERIODS = (("daily", "일간"), ("weekly", "주간"), ("monthly", "월간"))
STATISTIC_COLUMNS = {
    "activity": "DATE(c.closed_at + INTERVAL 9 HOUR)",
    "topics": "c.inquiry_type",
    "outcomes": "c.outcome",
    "duration": "DATE(c.closed_at + INTERVAL 9 HOUR)",
}


def dashboard_counts(start, end, today_start, today_end, channel=None):
    # 선택 기간의 문의 유형별 건수와 한국 기준 오늘의 시간대별 접수량을 집계한다.
    channel_args = (channel,) if channel in ("WEB", "PHONE") else ()
    scope = "WHERE opened_at>=%s AND opened_at<%s" + (" AND channel=%s" if channel_args else "")
    counts = rows("SELECT inquiry_type, COUNT(*) AS n FROM visible_case " + scope + " GROUP BY inquiry_type",
                  (start, end, *channel_args))
    hourly = rows("SELECT HOUR(opened_at + INTERVAL 9 HOUR) AS h, SUM(channel='WEB') AS web, SUM(channel='PHONE') AS phone "
                  "FROM visible_case " + scope + " GROUP BY h", (today_start, today_end, *channel_args))
    return counts, hourly


def channel_metrics(start, channel):
    # 채널별 종결·대기 건수, 최장 대기 시간과 평균 응답 시간을 집계한다.
    return one(
        "SELECT COUNT(CASE WHEN c.closed_at>=%s THEN 1 END) AS completed, "
        "SUM(c.closed_at IS NULL AND c.current_agent_id IS NULL) AS waiting, "
        "MAX(CASE WHEN c.closed_at IS NULL AND c.current_agent_id IS NULL "
        "THEN TIMESTAMPDIFF(SECOND,COALESCE(a.requeued,c.opened_at),UTC_TIMESTAMP(6)) END) AS longest, "
        "AVG(CASE WHEN a.accepted>=%s THEN TIMESTAMPDIFF(SECOND,c.opened_at,a.accepted) END) AS asa "
        "FROM visible_case c LEFT JOIN (SELECT case_id,MIN(IF(new_agent_id IS NOT NULL,changed_at,NULL)) AS accepted, "
        "MAX(IF(new_agent_id IS NULL,changed_at,NULL)) AS requeued FROM assignment_history GROUP BY case_id) a USING(case_id) "
        "WHERE c.channel=%s",
        (start, start, channel),
    )


def case_status_counts():
    # 상담을 대기·진행·종결 상태별로 센다.
    return rows(
        "SELECT CASE WHEN closed_at IS NOT NULL THEN 'closed' WHEN current_agent_id IS NULL THEN 'queued' "
        "ELSE 'active' END AS state, COUNT(*) AS n FROM visible_case GROUP BY state",
    )


def statistics(breakdown, start, end, *, staff_id=None, team_id=None, own_team=False):
    # 직원이나 팀의 종결 건수와 통화 시간을 지정한 기준으로 집계한다.
    scope = "c.current_agent_id=%s"
    # 팀 통계는 상담원의 현재 소속 팀이나 관리자가 선택한 팀을 기준으로 한다.
    if own_team:
        scope = "a.team_id=(SELECT team_id FROM staff_account WHERE staff_id=%s)"
    elif team_id:
        scope = "a.team_id=%s"
    voice = breakdown == "duration"
    return rows(
        f"SELECT {STATISTIC_COLUMNS[breakdown]} AS bucket, COUNT(*) AS n, SUM(c.channel='WEB') AS chats, SUM(c.channel='PHONE') AS calls, "
        "SUM(IF(c.call_direction='INBOUND',c.talk_seconds,NULL)) AS inbound, "
        "SUM(IF(c.call_direction='OUTBOUND',c.talk_seconds,NULL)) AS outbound, SUM(c.talk_seconds) AS seconds, "
        "COUNT(c.talk_seconds) AS timed FROM visible_case c JOIN staff_account a ON a.staff_id=c.current_agent_id "
        f"WHERE c.closed_at>=%s AND c.closed_at<%s AND {scope} " + ("AND c.channel='PHONE' " if voice else "") +
        "GROUP BY bucket ORDER BY " + ("bucket DESC" if breakdown in ("activity", "duration") else "n DESC,bucket"),
        (start, end, team_id or staff_id))


def call_history(staff_id, start, end):
    # 기간 내 담당 전화 상담의 통화 이력과 재생 가능한 녹취를 조회한다.
    return rows(
        "SELECT c.case_id,c.inquiry_type,c.call_direction,c.talk_seconds,c.closed_at, "
        "p.call_at AS call_time,r.recording_id,r.label,s.summary "
        "FROM visible_case c LEFT JOIN phone_response p USING(case_id) LEFT JOIN recording r "
        "ON r.case_id=c.case_id AND r.state='READY' "
        "LEFT JOIN ai_call_summary s ON s.recording_id=r.recording_id AND r.uploaded_by IS NULL AND s.state='READY' "
        "WHERE c.channel='PHONE' AND c.current_agent_id=%s AND COALESCE(p.call_at,c.opened_at)>=%s "
        "AND COALESCE(p.call_at,c.opened_at)<%s ORDER BY COALESCE(p.call_at,c.opened_at) DESC,c.case_id,r.stored_at DESC",
        (staff_id, start, end),
    )


def voice_counts_since(start):
    # 기준 시각 이후의 AI 통화 수와 상담원 인계 건수를 센다.
    return one(
        "SELECT COUNT(DISTINCT a.recording_id) AS sessions, COUNT(DISTINCT c.case_id) AS handed "
        "FROM audit_event a LEFT JOIN visible_case c ON c.intake_request_id=a.recording_id "
        "WHERE a.action IN ('VOICE_RECORDING_AGREED','VOICE_RECORDING_REFUSED') AND a.created_at>=%s",
        (start,),
    )


def quota_progress(starts, staff_id=None):
    # 상담원별 일·주·월 목표와 배정·종결 건수를 조회한다.
    people = rows(
        "SELECT a.staff_id, a.login_id, a.display_name, a.is_active, q.daily, q.weekly, q.monthly FROM staff_account a "
        "LEFT JOIN agent_quota q ON q.staff_id=a.staff_id WHERE a.role='AGENT' "
        + ("AND a.staff_id=%s " if staff_id else "") + "ORDER BY a.is_active DESC, a.login_id",
        (staff_id,) if staff_id else (),
    )
    for table, owner, at, metric in (
        ("assignment_history JOIN visible_case USING (case_id)", "new_agent_id", "changed_at", "assigned"),
        ("visible_case", "current_agent_id", "closed_at", "completed"),
    ):
        # 같은 상담은 한 번만 세며, 배정은 새 담당자, 종결은 현재 담당자 기준이다.
        counts = ", ".join(f"COUNT(DISTINCT IF({at}>=%s, case_id, NULL)) AS {key}" for key, _ in PERIODS)
        records = rows(
            f"SELECT {owner} AS staff_id, {counts} FROM {table} WHERE {at}>=%s "
            + (f"AND {owner}=%s " if staff_id else "") + f"GROUP BY {owner}",
            (*starts, min(starts), *((staff_id,) if staff_id else ())),
        )
        by_staff = {row["staff_id"]: row for row in records}
        for person in people:
            person[metric] = {key: by_staff.get(person["staff_id"], {}).get(key, 0) for key, _ in PERIODS}
    return people


def add_audit_event(action, actor, case_id, recording_id, result):
    # 작업 주체와 결과를 감사 로그에 기록한다.
    run(
        "INSERT INTO audit_event (action, actor, case_id, recording_id, result) "
        "VALUES (%s, %s, %s, %s, %s)",
        (action, actor, case_id, recording_id, result),
    )


def admin_changes(offset):
    # 다음 페이지 확인을 위해 관리자 변경 이력을 최대 41건 조회한다.
    return rows(
        "SELECT h.action,h.target,h.case_id,h.created_at,s.login_id,c.deleted_at FROM admin_change h "
        "JOIN staff_account s ON s.staff_id=h.actor_id LEFT JOIN support_case c ON c.case_id=h.case_id "
        "ORDER BY h.change_id DESC LIMIT 41 OFFSET %s",
        (offset,),
    )
