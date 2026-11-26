from calendar import monthrange
from datetime import date, timedelta

from flask import current_app

from .db import db
from .desk_reports import midnight, today
from .queries import usage


PERIODS = {"7d": "7일", "30d": "30일", "3m": "3개월", "6m": "6개월", "1y": "1년"}


def record_usage(channel, response):
    """OpenAI 응답의 사용량만 보관한다. 캐시 적중분은 입력 토큰에 포함된다."""
    if not isinstance(response, dict):
        return
    counts = response.get("usage")
    response_id = response.get("id")
    if not isinstance(counts, dict) or not isinstance(response_id, str) or not 0 < len(response_id) <= 180:
        return
    if "input_tokens" in counts:
        tokens_in, tokens_out = counts.get("input_tokens"), counts.get("output_tokens")
        details = counts.get("input_token_details")
    else:
        tokens_in, tokens_out = counts.get("prompt_tokens"), counts.get("completion_tokens")
        details = counts.get("prompt_tokens_details")
    cached = (details or {}).get("cached_tokens") or 0
    if not all(type(n) is int and n >= 0 for n in (tokens_in, tokens_out, cached)) or cached > tokens_in:
        return
    # 답변 저장이나 녹취 요약이 이후 취소되어도 이미 사용한 토큰은 남긴다.
    with current_app.app_context():
        usage.add(response_id, channel, tokens_in, cached, tokens_out)
        db().commit()


def range_start(period, end):
    if period in ("7d", "30d"):
        return end - timedelta(days=int(period[:-1]) - 1)
    until = end + timedelta(days=1)
    months = {"3m": 3, "6m": 6, "1y": 12}[period]
    year, month = divmod(until.year * 12 + until.month - 1 - months, 12)
    return date(year, month + 1, min(until.day, monthrange(year, month + 1)[1]))


def usage_report(period, channel=None):
    period = period if period in PERIODS else "3m"
    end = today()
    start = range_start(period, end)
    daily = {row["day"]: row for row in usage.daily(midnight(start), midnight(end + timedelta(days=1)), channel)}
    totals = dict(uncached=0, cached=0, output=0, responses=0)
    bars = {}
    day = start
    while day <= end:
        bucket = day
        if period == "3m":
            bucket = max(start, day - timedelta(days=day.weekday()))
        elif period in ("6m", "1y"):
            bucket = max(start, day.replace(day=1))
        if bucket not in bars:
            bars[bucket] = dict(day=bucket, uncached=0, cached=0, output=0, responses=0, total=0)
        bar = bars[bucket]
        bar["end"] = day
        if day in daily:
            for field in totals:
                value = int(daily[day][field])
                bar[field] += value
                totals[field] += value
            bar["total"] = bar["uncached"] + bar["cached"] + bar["output"]
        day += timedelta(days=1)
    inputs = totals["uncached"] + totals["cached"]
    return dict(**totals, total=inputs + totals["output"], hit_rate=round(totals["cached"] * 100 / inputs, 1) if inputs else None,
                bars=list(bars.values()), peak=max(1, max(bar["total"] for bar in bars.values())),
                period=period, periods=PERIODS, start=start, end=end, channel=channel)
