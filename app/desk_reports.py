from datetime import date, datetime, time, timedelta
from math import cos, sin, tau

from flask import request

from .common import ApiError
from .db import one, rows, utcnow

KST = timedelta(hours=9)
TOPICS = {"ACCOUNT": "계정·로그인", "FUNDS": "입출금", "TRADE": "거래", "GUIDE": "이용 안내"}
OUTCOMES = {"RESOLVED": "해결", "FOLLOWUP": "후속 안내"}
DIRECTIONS = {"INBOUND": "수신", "OUTBOUND": "발신"}
TOPIC_COLORS = ("#6459b4", "#dbb559", "#9e91d5", "#78a5a0", "#aaa7b6")


def today():
    return (utcnow() + KST).date()


def date_value(value):
    try:
        day = date.fromisoformat(value)
        if day.isoformat() != value or day.year < 1001 or day > today():
            raise ValueError
        return day
    except (ValueError, TypeError):
        raise ApiError("INVALID_INPUT") from None


def midnight(day):
    return datetime.combine(day, time.min) - KST


def report_range():
    end = date_value(request.args.get("end", today().isoformat()))
    start = date_value(request.args.get("start", (end - timedelta(days=6)).isoformat()))
    if start > end:
        raise ApiError("INVALID_INPUT")
    return {"start": start, "end": end, "today": today()}


def duration(seconds):
    if seconds is None:
        return "미기록"
    minutes, rest = divmod(round(seconds), 60)
    if not minutes:
        return f"{rest}초"
    return f"{minutes}분 {rest}초" if rest else f"{minutes}분"


def pie(labels, colors, counts):
    total = sum(counts.values())
    slices = []
    start = (50.0, 0.0)
    running = 0
    for (key, label), color in zip(labels.items(), colors):
        count = counts.get(key, 0)
        running += count
        sector = None
        if 0 < count < total:
            angle = tau * running / total
            end = (50 + 50 * sin(angle), 50 - 50 * cos(angle))
            sector = (f"M 50 50 L {start[0]:.3f} {start[1]:.3f} "
                      f"A 50 50 0 {int(count * 2 > total)} 1 {end[0]:.3f} {end[1]:.3f} Z")
            start = end
        slices.append(dict(label=label, color=color, count=count, percent=count * 100 / total if total else 0, sector=sector))
    return {"total": total, "slices": slices}


def dashboard_charts():
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
    counts = rows("SELECT inquiry_type, COUNT(*) AS n FROM visible_case WHERE opened_at>=%s AND opened_at<%s GROUP BY inquiry_type",
                  (midnight(start), midnight(end)))
    hourly = rows("SELECT HOUR(opened_at + INTERVAL 9 HOUR) AS h, SUM(channel='WEB') AS web, SUM(channel='PHONE') AS phone "
                  "FROM visible_case WHERE opened_at>=%s AND opened_at<%s GROUP BY h", (midnight(current), midnight(current + timedelta(days=1))))
    slots = [{"web": 0, "phone": 0} for _ in range(24)]
    for row in hourly:
        slots[row["h"]] = {"web": int(row["web"]), "phone": int(row["phone"])}
    return dict(unit=unit, day=start, month=start.isoformat()[:7], today=current,
                topics=pie({**TOPICS, None: "미분류"}, TOPIC_COLORS, {r["inquiry_type"]: r["n"] for r in counts}),
                slots=slots, peak=max(1, *(r["web"] + r["phone"] for r in slots)), arrivals=sum(r["web"] + r["phone"] for r in slots))


def channel_metrics(kind):
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


def chat_breakdown(staff_id):
    data = rows("SELECT CASE WHEN c.closed_at IS NOT NULL THEN 'closed' WHEN c.current_agent_id IS NULL THEN 'queued' "
                "ELSE 'active' END AS state,COUNT(*) AS n FROM visible_case c LEFT JOIN staff_account a ON a.staff_id=c.current_agent_id "
                "WHERE c.channel='WEB' AND (c.current_agent_id IS NULL OR c.current_agent_id=%s OR "
                "a.team_id=(SELECT team_id FROM staff_account WHERE staff_id=%s)) GROUP BY state", (staff_id, staff_id))
    return pie({"queued": "대기", "active": "진행", "closed": "종결"}, ("#dbb559", "#9e91d5", "#6459b4"), {r["state"]: r["n"] for r in data})


def notices(latest=False):
    return rows("SELECT n.notice_id,n.title,n.body,n.published_at,COALESCE(a.display_name,a.login_id) AS author "
                "FROM staff_notice n JOIN staff_account a ON a.staff_id=n.author_id ORDER BY published_at DESC,notice_id DESC"
                + (" LIMIT 1" if latest else ""))


def statistics(dates, *, staff_id=None, team_id=None, own_team=False):
    breakdown = request.args.get("by", "activity")
    columns = {"activity": "DATE(c.closed_at + INTERVAL 9 HOUR)", "topics": "c.inquiry_type",
               "outcomes": "c.outcome", "duration": "DATE(c.closed_at + INTERVAL 9 HOUR)"}
    if breakdown not in columns:
        raise ApiError("INVALID_INPUT")
    scope = "c.current_agent_id=%s"
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
        if voice:
            return [duration(row[k]) for k in ("inbound", "outbound", "seconds")] + [f"{row['timed']} / {row['n']}건"]
        return [f"{int(row[k])}건" for k in ("calls", "chats", "n")]
    totals = {}
    for key in ("n", "chats", "calls", "inbound", "outbound", "seconds", "timed"):
        values = [r[key] for r in records if r[key] is not None]
        totals[key] = sum(values) if values else None
    names = TOPICS if breakdown == "topics" else OUTCOMES
    lines = [(names.get(r["bucket"], r["bucket"] or ("미분류" if breakdown == "topics" else "미기록")), cells(r)) for r in records]
    return dict(by=breakdown, lines=lines, total=cells(totals) if records else None, count=totals["n"] or 0, voice=voice)


def call_history(staff_id, dates):
    # A scoped case is the unit of the history, even if it has zero or multiple recordings.
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
