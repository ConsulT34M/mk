# AI TM 상담 데스크

거래소 고객의 웹채팅과 모의 전화 상담을 처리하는 수탁사 직원 포털입니다. Docker Compose에서 Nginx, Flask와 MySQL 8.4를 실행합니다.

## 실행

Docker Desktop의 Linux 컨테이너 엔진과 Python 3에서 프로젝트 폴더를 열고 실행합니다.

```powershell
python setup_demo.py
docker compose up -d --build --wait
```

직원 포털은 [https://localhost:8443](https://localhost:8443), 고객 채팅은 [https://localhost:8443/chat](https://localhost:8443/chat)입니다. 첫 실행 시 Nginx가 로컬용 자체 서명 인증서를 생성합니다. `docker compose stop`으로 중지하고 `docker compose start`로 다시 시작합니다.

## 시연

| 로그인 아이디 | 비밀번호가 있는 로컬 파일 | 역할 |
|---|---|---|
| `admin` | `.env`의 `DEMO_ADMIN_PASSWORD` | 전화 접수, 상담 배정·재배정 |
| `agent1`, `agent2` | `.env`의 `DEMO_AGENT1_PASSWORD`, `DEMO_AGENT2_PASSWORD` | 현재 배정된 상담의 응대·종결·녹취 재생 |

1. 관리자로 로그인합니다. 다른 브라우저 프로필에서는 `agent1`으로 로그인합니다.
2. 아래 명령으로 가상 고객 1번의 입장 코드를 받고 고객 채팅 화면에 붙여넣습니다. 관리자는 고객 화면에 표시된 상담 번호를 찾아 `agent1`에 배정합니다. 상담원은 ‘내 상담’에서 같은 건을 열어 대화합니다. `--customer 2`로 고객을 바꾸거나 `--new`를 추가해 새 상담을 접수할 수 있습니다.

   ```powershell
   docker compose exec -T app flask demo-entry --customer 1
   ```

3. 관리자가 전화 상담을 접수·배정하면 담당 상담원이 통화 일시·문의·안내·후속 조치·녹취 동의 결과를 저장합니다. 동의한 건에는 `setup_demo.py`가 생성한 `.local/sample.wav`를 등록할 수 있습니다. 거절한 건은 메모로 종결합니다.
4. 전화 상담을 종결하기 전에 관리자가 `agent2`에게 재배정하면 다음 요청부터 새 담당자만 녹취를 재생할 수 있습니다. 상담원이 처리 결과를 입력해 종결하면 기록은 읽기 전용이 됩니다.

## 거래소 연동

거래소 서버가 보낼 HTTP 헤더는 `Authorization: Bearer <PARTNER_TOKEN>`입니다. 토큰은 로컬 `.env`에 있습니다.

| 요청 | 용도 |
|---|---|
| `POST /cases` | `support_subject_ref`, `intake_request_id` 두 UUID로 접수합니다. 같은 접수는 같은 `case_id`를 반환합니다. |
| `GET /cases?support_subject_ref=…` | 해당 고객의 웹 상담 상태 목록을 조회합니다. |
| `POST /cases/{case_id}/entry-codes` | 본문에 `support_subject_ref`를 보내 일회용 `entry_code`를 발급받습니다. |

고객 브라우저에서 포털의 `/chat/enter`로 `entry_code`를 HTML 폼 POST하면 방 전용 쿠키를 발급하고 채팅으로 이동합니다. 포털과 같은 출처의 클라이언트는 `POST /chat/sessions`에 같은 필드를 JSON으로 보낼 수 있습니다. [Notion API 명세](https://app.notion.com/p/f160de8f474583b083d281a75e3f3f80)

## 저장과 만료

녹취 원본은 비공개 Docker 볼륨 `recordings`에 보관하고 Flask가 현재 담당자와 만료 시각을 확인한 뒤 전송합니다. S3는 후속 이전 대상이고 AI 상담 요약은 2차 범위입니다.

| 로컬 설정 | 기본값 |
|---|---|
| `STAFF_SESSION_MINUTES` / `CHAT_SESSION_MINUTES` / `ENTRY_CODE_MINUTES` | 발급 후 각각 30분 / 60분 / 5분 |
| `WAV_MAX_MIB` | PCM WAV 최대 10 MiB |
| `RECORDING_RETENTION_MINUTES` / `CASE_RETENTION_MINUTES` | 각각 녹취 저장 완료 / 상담 종결 후 5분 |

설정을 바꾸려면 `.env`에 값을 추가하고 `docker compose up -d`를 실행합니다. 만료 시 접근을 즉시 차단하고 30초마다 원본과 연결 정보 또는 상담 내용을 정리합니다. MySQL의 `assignment_history`와 `audit_event`에서 배정·조회·재생·파기 이력을 확인할 수 있습니다.

Git에는 소스와 실행 구성을 올립니다. 생성된 `.env`, 샘플 WAV, 로컬 인증서와 실제 녹취는 제외됩니다.

## 통합 검증

별도 시연 환경을 띄운 뒤 아래 명령을 실행하면 합성 상담 데이터를 생성해 거래소 접수, 채팅, 배정, 녹취 동의·재생·종결을 검증합니다. `--port`에는 해당 환경이 공개한 포트를 지정합니다.

```powershell
docker compose cp nginx:/etc/nginx/local-tls/cert.pem .local/cert.pem
python tests/smoke.py --port 8443 --cert .local/cert.pem
```
