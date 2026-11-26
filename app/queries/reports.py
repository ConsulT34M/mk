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
    channel_args = (channel,) if channel in ("WEB", "PHONE") else ()
    scope = "WHERE opened_at>=%s AND opened_at<%s" + (" AND channel=%s" if channel_args else "")
    counts = rows("SELECT inquiry_type, COUNT(*) AS n FROM visible_case " + scope + " GROUP BY inquiry_type",
                  (start, end, *channel_args))
    # 시간대별 접수량은 선택한 분포 기간과 별개로 항상 한국의 오늘을 0~23시로 나눈다.
    # 접수가 없는 시간도 두 채널 모두 0으로 채워 템플릿이 24칸을 그릴 수 있게 한다.
    hourly = rows("SELECT HOUR(opened_at + INTERVAL 9 HOUR) AS h, SUM(channel='WEB') AS web, SUM(channel='PHONE') AS phone "
                  "FROM visible_case " + scope + " GROUP BY h", (today_start, today_end, *channel_args))
    return counts, hourly


def channel_metrics(start, channel):
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
    return rows(
        "SELECT CASE WHEN closed_at IS NOT NULL THEN 'closed' WHEN current_agent_id IS NULL THEN 'queued' "
        "ELSE 'active' END AS state, COUNT(*) AS n FROM visible_case GROUP BY state",
    )


def statistics(breakdown, start, end, *, staff_id=None, team_id=None, own_team=False):
    scope = "c.current_agent_id=%s"
    # 상담원 개인 화면은 본인 담당 건, 팀 보기는 해당 직원의 현재 소속 팀,
    # 관리자 조직 화면은 관리자가 선택한 팀 ID로 조회 범위를 정한다.
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
    return rows(
        "SELECT c.case_id,c.inquiry_type,c.call_direction,c.talk_seconds,c.closed_at, "
        "COALESCE(p.call_at,c.opened_at) AS call_time, r.recording_id,r.label "
        "FROM visible_case c LEFT JOIN phone_response p USING(case_id) LEFT JOIN recording r "
        "ON r.case_id=c.case_id AND r.state='READY' AND r.expires_at>UTC_TIMESTAMP(6) "
        "WHERE c.channel='PHONE' AND c.current_agent_id=%s AND COALESCE(p.call_at,c.opened_at)>=%s "
        "AND COALESCE(p.call_at,c.opened_at)<%s ORDER BY call_time DESC,c.case_id,r.stored_at DESC",
        (staff_id, start, end),
    )


def voice_counts_since(start):
    return one(
        "SELECT COUNT(DISTINCT a.recording_id) AS sessions, COUNT(DISTINCT c.case_id) AS handed "
        "FROM audit_event a LEFT JOIN visible_case c ON c.intake_request_id=a.recording_id "
        "WHERE a.action IN ('VOICE_RECORDING_AGREED','VOICE_RECORDING_REFUSED') AND a.created_at>=%s",
        (start,),
    )


def quota_progress(starts, staff_id=None):
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
        # 배정 건수는 배정 이력의 새 담당자 기준, 종결 건수는 티켓의 현재 담당자 기준이다.
        # 같은 티켓이 한 기간에 여러 번 배정돼도 상담원별로 한 건만 세고 실적이 없으면 0을 채운다.
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
    # 조회·재생·정리 같은 이벤트의 주체와 결과를 기록한다. 대상이 있는 경우에만 연관 ID를 남긴다.
    run(
        "INSERT INTO audit_event (action, actor, case_id, recording_id, result) "
        "VALUES (%s, %s, %s, %s, %s)",
        (action, actor, case_id, recording_id, result),
    )


def admin_changes(offset):
    return rows(
        "SELECT h.action,h.target,h.case_id,h.created_at,s.login_id,c.deleted_at FROM admin_change h "
        "JOIN staff_account s ON s.staff_id=h.actor_id LEFT JOIN support_case c ON c.case_id=h.case_id "
        "ORDER BY h.change_id DESC LIMIT 41 OFFSET %s",
        (offset,),
    )
