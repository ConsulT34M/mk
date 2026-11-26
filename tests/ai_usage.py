"""격리된 MySQL에서 응답 중복, 채널 합계 및 KST 집계 경계를 검사한다."""

import os
from datetime import date, timedelta
from uuid import uuid4
from unittest.mock import patch

from app import app
from app.ai_usage import range_start, record_usage, usage_report
from app.db import db, run
from app.desk_reports import midnight, today


def check_usage():
    admin, agent = app.test_client(), app.test_client()
    for client, login in ((admin, "admin"), (agent, "agent1")):
        client.post("/staff/sessions", json={"login_id": login, "password": os.environ["DEMO_" + login.upper() + "_PASSWORD"]})
    with app.app_context():
        chat = {"id": str(uuid4()), "usage": {"prompt_tokens": 100, "completion_tokens": 25, "prompt_tokens_details": {"cached_tokens": 70}}}
        voice = {"id": str(uuid4()), "usage": {"input_tokens": 40, "output_tokens": 8, "input_token_details": {"cached_tokens": 12}}}
        end = today() - timedelta(days=450)
        with patch("app.ai_usage.today", return_value=end):
            boundary = midnight(end - timedelta(days=6))
            for instant, event, channel in ((boundary, chat, "CHAT"), (midnight(end), voice, "VOICE"),
                                            (boundary-timedelta(microseconds=1), dict(chat, id=str(uuid4())), "CHAT")):
                record_usage(channel, event)
                record_usage(channel, event)
                run("UPDATE ai_token_usage SET used_at=%s WHERE response_id=%s", (instant,event["id"]))
                db().commit()
            text = usage_report("7d", "CHAT")
            call = usage_report("7d", "VOICE")
            both = usage_report("7d")
            assert (text["uncached"],text["cached"],text["output"],text["responses"]) == (30,70,25,1)
            assert (call["total"],call["cached"],call["output"],call["hit_rate"]) == (48,12,8,30)
            assert both["total"] == text["total"] + call["total"] == 173
            assert sum(bar["total"] for bar in both["bars"]) == 173
            assert both["bars"][0]["total"] == 125 and both["bars"][-1]["total"] == 48
            assert both["hit_rate"] == 58.6
            assert range_start("1y",date(2024,2,29)) == date(2023,3,1)
            for tab, count, after in (("overview","173","오늘 상담원별 처리 현황"),("ai-chats","125","RKX AI 채팅 내역"),
                                      ("ai-calls","48","RKX 전화 상담")):
                page = admin.get("/admin", query_string={"tab":tab,"usage_period":"7d"})
                html = page.get_data(as_text=True)
                assert page.status_code == 200 and f'data-tokens="total">{count}<' in html
                assert html.index('id="token-usage"') < html.index(after)
                assert 'id="tickets"' not in html
            assert agent.get("/admin?tab=ai-chats").status_code == 403
    for tab in ("chats", "calls"):
        html = admin.get("/admin?tab="+tab).get_data(as_text=True)
        assert 'id="tickets"' in html and 'id="token-usage"' not in html


if __name__ == "__main__":
    check_usage()
    print("PASS usage per channel and combined, KST cutoff, deduplication, cache share and admin charts")
