"""모델 응답별 사용량과 한국 날짜별 합계."""

from ..db import rows, run


def add(response_id, channel, input_tokens, cached_tokens, output_tokens):
    run("INSERT INTO ai_token_usage (response_id,channel,input_tokens,cached_tokens,output_tokens) "
        "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE response_id=ai_token_usage.response_id",
        (response_id, channel, input_tokens, cached_tokens, output_tokens))


def daily(start, end, channel):
    condition = " AND channel=%s" if channel else ""
    args = (start, end, channel) if channel else (start, end)
    return rows("SELECT DATE(used_at + INTERVAL 9 HOUR) AS day, COUNT(*) AS responses, "
                "SUM(input_tokens-cached_tokens) AS uncached, SUM(cached_tokens) AS cached, SUM(output_tokens) AS output "
                "FROM ai_token_usage WHERE used_at >= %s AND used_at < %s" + condition + " GROUP BY day ORDER BY day", args)
