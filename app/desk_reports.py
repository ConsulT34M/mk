from datetime import date, datetime, time, timedelta
from math import cos, sin, tau

from flask import request

from .common import ApiError
from .db import one, rows, utcnow

# 화면의 달력과 일별 집계는 한국 시간을 기준으로 한다. MySQL에 저장한 일시는 UTC다.
KST = timedelta(hours=9)
TOPICS = {"ACCOUNT": "계정·로그인", "FUNDS": "입출금", "TRADE": "거래", "GUIDE": "이용 안내"}
OUTCOMES = {"RESOLVED": "해결", "FOLLOWUP": "후속 안내"}
DIRECTIONS = {"INBOUND": "수신", "OUTBOUND": "발신"}
TOPIC_COLORS = ("#5189ae", "#54b4bc", "#e4a355", "#88a379", "#97a4ae")


def today():
    # 서버 운영체제의 시간대와 관계없이 화면에서 '오늘'로 표시할 한국 날짜를 구한다.
    return (utcnow() + KST).date()


def date_value(value):
    # 날짜 선택기의 YYYY-MM-DD 표기인지 확인한다. 1001년부터 한국 기준 오늘까지의 날짜를 허용하고
    # 형식이나 범위가 맞지 않으면 공통 입력 오류로 응답한다.
    try:
        day = date.fromisoformat(value)
        if day.isoformat() != value or day.year < 1001 or day > today():
            raise ValueError
        return day
    except (ValueError, TypeError):
        raise ApiError("INVALID_INPUT") from None


def midnight(day):
    # 사용자가 고른 날짜의 한국 자정을 DB의 UTC 시각으로 옮긴다. 한국 시간 00시는 UTC로 전날 15시다.
    return datetime.combine(day, time.min) - KST


def report_range():
    # start와 end는 모두 조회에 포함할 날짜다. 기본 범위는 종료일을 포함한 최근 7일이며,
    # 실제 쿼리는 마지막 날 전체를 포함하도록 다음 날 자정 미만까지 읽는다.
    end = date_value(request.args.get("end", today().isoformat()))
    start = date_value(request.args.get("start", (end - timedelta(days=6)).isoformat()))
    if start > end:
        raise ApiError("INVALID_INPUT")
    return {"start": start, "end": end, "today": today()}


def duration(seconds):
    # 미입력(None)과 실제 0초를 구분한다. 평균값처럼 소수일 수 있는 초는 반올림해 분·초로 표시한다.
    if seconds is None:
        return "미기록"
    minutes, rest = divmod(round(seconds), 60)
    if not minutes:
        return f"{rest}초"
    return f"{minutes}분 {rest}초" if rest else f"{minutes}분"


def pie(labels, colors, counts):
    # 집계 결과를 Jinja의 SVG 차트와 범례가 함께 쓸 항목 목록으로 만든다. 건수가 없는 분류도
    # 범례에 남기고, 전체가 0건일 때에는 모든 비율을 0으로 표시한다.
    total = sum(counts.values())
    slices = []
    start = (50.0, 0.0)
    running = 0
    for (key, label), color in zip(labels.items(), colors):
        count = counts.get(key, 0)
        running += count
        sector = None
        if 0 < count < total:
            # 원의 맨 위에서 시작해 누적 비율만큼 시계 방향으로 이동한다. 절반을 넘는 조각은
            # 긴 원호를 사용한다. 한 분류가 100%면 템플릿이 원 전체를 그리므로 경로를 생략한다.
            angle = tau * running / total
            end = (50 + 50 * sin(angle), 50 - 50 * cos(angle))
            sector = (f"M 50 50 L {start[0]:.3f} {start[1]:.3f} "
                      f"A 50 50 0 {int(count * 2 > total)} 1 {end[0]:.3f} {end[1]:.3f} Z")
            start = end
        slices.append(dict(label=label, color=color, count=count, percent=count * 100 / total if total else 0, sector=sector))
    return {"total": total, "slices": slices}


def dashboard_charts(channel=None):
    # 문의 유형별 분포에 적용할 날짜 또는 달을 쿼리에서 읽는다. 월별 범위의 끝은 다음 달 1일이어서
    # 달의 길이와 윤년에 관계없이 선택한 달만 집계한다.
    current = today()
    unit = request.args.get("unit", "day")
    if unit == "month":
        start = date_value(request.args.get("month", current.isoformat()[:7]) + "-01")
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    elif unit == "day":
        start = date_value(request.args.get("day", current.isoformat()))
        end = start + timedelta(days=1)
    else:
        raise ApiError("INVALID_INPUT")
    channel_args = (channel,) if channel in ("WEB", "PHONE") else ()
    scope = "WHERE opened_at>=%s AND opened_at<%s" + (" AND channel=%s" if channel_args else "")
    counts = rows("SELECT inquiry_type, COUNT(*) AS n FROM visible_case " + scope + " GROUP BY inquiry_type",
                  (midnight(start), midnight(end), *channel_args))
    # 시간대별 접수량은 선택한 분포 기간과 별개로 항상 한국의 오늘을 0~23시로 나눈다.
    # 접수가 없는 시간도 두 채널 모두 0으로 채워 템플릿이 24칸을 그릴 수 있게 한다.
    hourly = rows("SELECT HOUR(opened_at + INTERVAL 9 HOUR) AS h, SUM(channel='WEB') AS web, SUM(channel='PHONE') AS phone "
                  "FROM visible_case " + scope + " GROUP BY h", (midnight(current), midnight(current + timedelta(days=1)), *channel_args))
    slots = [{"web": 0, "phone": 0} for _ in range(24)]
    for row in hourly:
        slots[row["h"]] = {"web": int(row["web"]), "phone": int(row["phone"])}
    return dict(unit=unit, day=start, month=start.isoformat()[:7], today=current,
                topics=pie({**TOPICS, None: "미분류"}, TOPIC_COLORS, {r["inquiry_type"]: r["n"] for r in counts}),
                slots=slots, peak=max(1, *(max(row.values()) for row in slots)), arrivals=sum(r["web"] + r["phone"] for r in slots))


def channel_metrics(kind):
    # 선택 채널의 오늘 종결 건수와 현재 대기 건수를 센다. 최장 대기는 마지막 배정 해제 또는 접수부터,
    # 평균 응답 시간(asa)은 오늘 처음 배정된 티켓의 최초 접수부터 최초 배정까지로 계산한다.
    start = midnight(today())
    result = one(
        "SELECT COUNT(CASE WHEN c.closed_at>=%s THEN 1 END) AS completed, "
        "SUM(c.closed_at IS NULL AND c.current_agent_id IS NULL) AS waiting, "
        "MAX(CASE WHEN c.closed_at IS NULL AND c.current_agent_id IS NULL "
        "THEN TIMESTAMPDIFF(SECOND,COALESCE(a.requeued,c.opened_at),UTC_TIMESTAMP(6)) END) AS longest, "
        "AVG(CASE WHEN a.accepted>=%s THEN TIMESTAMPDIFF(SECOND,c.opened_at,a.accepted) END) AS asa "
        "FROM visible_case c LEFT JOIN (SELECT case_id,MIN(IF(new_agent_id IS NOT NULL,changed_at,NULL)) AS accepted, "
        "MAX(IF(new_agent_id IS NULL,changed_at,NULL)) AS requeued FROM assignment_history GROUP BY case_id) a USING(case_id) "
        "WHERE c.channel=%s", (start, start, kind))
    return dict(completed=result["completed"], waiting=int(result["waiting"] or 0), longest=duration(result["longest"] or 0),
                asa=duration(result["asa"]) if result["asa"] is not None else "—")


def case_status_breakdown():
    # 전체 티켓을 종결·대기·진행 중 하나로만 분류한다. 상태별 건수와 비율을 상담원 대시보드에 보낸다.
    counts = rows(
        "SELECT CASE WHEN closed_at IS NOT NULL THEN 'closed' WHEN current_agent_id IS NULL THEN 'queued' "
        "ELSE 'active' END AS state, COUNT(*) AS n FROM visible_case GROUP BY state"
    )
    return pie({"queued": "대기", "active": "진행 중", "closed": "종결"},
               ("#d29a48", "#50a9b5", "#68869a"), {row["state"]: row["n"] for row in counts})


def notices(latest=False):
    # 최근 공지가 먼저 오도록 정렬한다. 데스크 요약은 한 건만, 공지 탭은 전체 목록을 사용한다.
    return rows("SELECT n.notice_id,n.title,n.body,n.published_at,COALESCE(a.display_name,a.login_id) AS author "
                "FROM staff_notice n JOIN staff_account a ON a.staff_id=n.author_id ORDER BY published_at DESC,notice_id DESC"
                + (" LIMIT 1" if latest else ""))


def statistics(dates, *, staff_id=None, team_id=None, own_team=False):
    # 선택 기간에 종결된 티켓을 날짜·문의 유형·결과별로 묶는다. 통화 시간 보기는 전화 티켓만
    # 날짜별로 집계한다. 쿼리의 by 값은 아래 사전을 거쳐 정해진 SQL 열로만 바뀐다.
    breakdown = request.args.get("by", "activity")
    columns = {"activity": "DATE(c.closed_at + INTERVAL 9 HOUR)", "topics": "c.inquiry_type",
               "outcomes": "c.outcome", "duration": "DATE(c.closed_at + INTERVAL 9 HOUR)"}
    if breakdown not in columns:
        raise ApiError("INVALID_INPUT")
    scope = "c.current_agent_id=%s"
    # 상담원 개인 화면은 본인 담당 건, 팀 보기는 해당 직원의 현재 소속 팀,
    # 관리자 조직 화면은 관리자가 선택한 팀 ID로 조회 범위를 정한다.
    if own_team:
        scope = "a.team_id=(SELECT team_id FROM staff_account WHERE staff_id=%s)"
    elif team_id:
        scope = "a.team_id=%s"
    voice = breakdown == "duration"
    records = rows(
        f"SELECT {columns[breakdown]} AS bucket, COUNT(*) AS n, SUM(c.channel='WEB') AS chats, SUM(c.channel='PHONE') AS calls, "
        "SUM(IF(c.call_direction='INBOUND',c.talk_seconds,NULL)) AS inbound, "
        "SUM(IF(c.call_direction='OUTBOUND',c.talk_seconds,NULL)) AS outbound, SUM(c.talk_seconds) AS seconds, "
        "COUNT(c.talk_seconds) AS timed FROM visible_case c JOIN staff_account a ON a.staff_id=c.current_agent_id "
        f"WHERE c.closed_at>=%s AND c.closed_at<%s AND {scope} " + ("AND c.channel='PHONE' " if voice else "") +
        "GROUP BY bucket ORDER BY " + ("bucket DESC" if breakdown in ("activity", "duration") else "n DESC,bucket"),
        (midnight(dates["start"]), midnight(dates["end"] + timedelta(days=1)), team_id or staff_id))
    def cells(row):
        # 통화 표에는 방향별 시간과 길이를 입력한 건수/전체 건수를, 그 외 표에는 채널별 건수를 표시한다.
        if voice:
            return [duration(row[k]) for k in ("inbound", "outbound", "seconds")] + [f"{row['timed']} / {row['n']}건"]
        return [f"{int(row[k])}건" for k in ("calls", "chats", "n")]
    totals = {}
    # 길이가 기록되지 않은 값(NULL)은 합계에서도 제외한다. 전부 미기록이면 합계 역시 '미기록'이다.
    for key in ("n", "chats", "calls", "inbound", "outbound", "seconds", "timed"):
        values = [r[key] for r in records if r[key] is not None]
        totals[key] = sum(values) if values else None
    names = TOPICS if breakdown == "topics" else OUTCOMES
    lines = [(names.get(r["bucket"], r["bucket"] or ("미분류" if breakdown == "topics" else "미기록")), cells(r)) for r in records]
    return dict(by=breakdown, lines=lines, total=cells(totals) if records else None, count=totals["n"] or 0, voice=voice)


def call_history(staff_id, dates):
    # 담당 전화 티켓을 기준으로 통화 이력을 만든다. 통화 일시가 비어 있으면 접수 시각으로 기간을 판단한다.
    # LEFT JOIN을 써서 재생할 녹취가 없는 티켓도 남기고, 녹취가 여러 개인 티켓은 아래에서 다시 묶는다.
    records = rows("SELECT c.case_id,c.inquiry_type,c.call_direction,c.talk_seconds,c.closed_at, "
                   "COALESCE(p.call_at,c.opened_at) AS call_time, r.recording_id,r.label "
                   "FROM visible_case c LEFT JOIN phone_response p USING(case_id) LEFT JOIN recording r "
                   "ON r.case_id=c.case_id AND r.state='READY' AND r.expires_at>UTC_TIMESTAMP(6) "
                   "WHERE c.channel='PHONE' AND c.current_agent_id=%s AND COALESCE(p.call_at,c.opened_at)>=%s "
                   "AND COALESCE(p.call_at,c.opened_at)<%s ORDER BY call_time DESC,c.case_id,r.stored_at DESC",
                   (staff_id, midnight(dates["start"]), midnight(dates["end"] + timedelta(days=1))))
    history = {}
    for row in records:
        case = history.setdefault(row["case_id"], {**row, "clips": []})
        if row["recording_id"]:
            case["clips"].append((row["recording_id"], row["label"] or "녹취 듣기"))
    return list(history.values())
