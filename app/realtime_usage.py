"""WebRTC 통화의 모델 응답 사용량을 OpenAI에서 직접 수신한다."""

import json
from contextlib import closing
from threading import Event, Thread
from time import monotonic, sleep

from websocket import WebSocketException, WebSocketTimeoutException, create_connection

from .ai_usage import record_usage
from .queries import voice


def watch_call(app, voice_id, call_id, key):
    ready = Event()

    def active():
        with app.app_context():
            return voice.session_exists(voice_id)

    def listen():
        limit = monotonic() + 65 * 60
        for attempt in range(4):
            if attempt:
                sleep(attempt)
                if (attempt > 1 or ready.is_set()) and not active():
                    return
            try:
                with closing(create_connection("wss://api.openai.com/v1/realtime?call_id=" + call_id,
                             header={"Authorization": "Bearer " + key},
                             suppress_origin=True, timeout=5)) as ws:
                    ready.set()
                    ws.settimeout(20)
                    while monotonic() < limit:
                        try:
                            raw = ws.recv()
                        except WebSocketTimeoutException:
                            if not active():
                                return
                            continue
                        if not raw:
                            break
                        event = json.loads(raw)
                        if event.get("type") == "response.done":
                            with app.app_context():
                                record_usage("VOICE", event["response"])
                    else:
                        return
            except (OSError, WebSocketException):
                if attempt == 3:
                    app.logger.warning("AI 음성 사용량 연결을 복구하지 못했습니다: %s", voice_id)

    Thread(target=listen, daemon=True, name="voice-usage").start()
    # 첫 인사 응답부터 수신하도록 연결을 기다리되 통화 시작을 무기한 막지 않는다.
    ready.wait(2)
