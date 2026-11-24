# AI TM 상담 데스크

거래소 고객의 웹채팅과 모의 전화 상담을 처리하는 수탁사 직원 포털입니다. Docker Compose에서 Nginx, Flask와 MySQL 8.4를 실행합니다.

## 폴더 구성

- `app/`에 Flask 코드가 있습니다. 화면 템플릿은 `app/templates/`, 스타일·스크립트·서비스 로고는 `app/static/`, 상담 데스크 시안은 `app/static/prototype/`에 있습니다.
- `mysql/schema.sql`은 데이터베이스 초기화 파일이고, `nginx/`에는 HTTPS 설정이 있습니다.
- `tests/`에는 상담 흐름 통합 테스트가 있습니다. 로컬 디자인 원본은 `design/`에 보관합니다.

## 실행

Docker Desktop의 Linux 컨테이너 엔진과 Python 3에서 프로젝트 폴더를 열고 실행합니다.

```powershell
python setup_demo.py
docker compose up -d --build --wait
```

직원 포털은 [https://localhost:8443](https://localhost:8443), 고객 채팅은 [https://localhost:8443/chat](https://localhost:8443/chat)입니다. 첫 실행 시 Nginx가 로컬용 자체 서명 인증서를 생성합니다. `docker compose stop`으로 중지하고 `docker compose start`로 다시 시작합니다.

## 로컬 시연 계정

| 역할 | 아이디 | 비밀번호 | 로그인 후 화면 |
|---|---|---|---|
| 관리자 | `admin` | `admin` | [관리자 대시보드](https://localhost:8443/admin) |
| 상담원 | `test` | `test` | [상담원 포털](https://localhost:8443/portal) |

새 로컬 환경의 기본값입니다. 새 DB를 만들 때 `.env`에 지정한 `DEMO_ADMIN_PASSWORD`를 관리자 초기 비밀번호로 사용하며, 기존 DB의 계정과 비밀번호는 재시작 후에도 유지됩니다. 재배정 시연용 상담원 `agent1`, `agent2`의 비밀번호는 `.env`의 `DEMO_AGENT1_PASSWORD`, `DEMO_AGENT2_PASSWORD`에 있습니다.

## 티켓 접수와 담당자 변경

1. 서로 다른 브라우저 프로필에서 관리자와 상담원으로 로그인합니다.
2. 외부 서버가 `POST /cases`로 웹채팅 또는 전화 요청을 보내면 미배정 티켓이 생성됩니다. 아래 명령으로 외부 요청을 시연할 수 있습니다. 첫 번째 명령은 고객 채팅 입장 코드를, 두 번째 명령은 새 전화 티켓 번호를 출력합니다.

   ```powershell
   docker compose exec -T app flask demo-entry --customer 1 --new
   docker compose exec -T app flask demo-entry --customer 1 --channel PHONE --new
   ```

3. 상담원 대시보드에서 ‘상담 받기’를 먼저 누른 상담원이 티켓을 가져갑니다. 고객은 입장 코드로 채팅에 참여합니다. 전화 상담의 담당자는 응대 내용과 녹취 동의 여부를 기록하고, 동의한 건에 `.local/sample.wav`를 등록할 수 있습니다.
4. 수락한 티켓은 관리자 대시보드에 ‘진행 중’으로 표시됩니다. 관리자는 이때 ‘담당자 변경’으로 다른 활성 상담원에게 재배정할 수 있습니다. 이후 새 담당자가 이어서 응대하고 처리 결과를 입력해 종결합니다.

## 관리자 페이지 (F07)

`/admin` 대시보드에는 전체·대기·진행·종결 티켓 수, 담당자, 오늘 상담원별 처리 건수와 잔여 목표가 표시됩니다.

| 요구사항 | 메뉴 | 관리자 작업 |
|---|---|---|
| FR-026 | 상담원 계정 | 계정 생성·조회, 아이디 및 비밀번호 수정, 계정 삭제(비활성화)와 재활성화. 비활성화하면 세션이 종료되고 진행 중인 티켓은 대기열로 돌아갑니다. |
| FR-027 | 상담 기록 → 웹채팅 | 대화 조회, 개별 메시지 수정·삭제, 상담 기록 전체 삭제. 변경 사항은 고객과 상담원의 대화에도 반영됩니다. |
| FR-028 | 상담 기록 → 녹취 | 파일 상태·크기·보관 기한 조회, 표시명 수정, 녹취 삭제. 삭제 요청 시 재생이 즉시 차단되고 파기 작업이 원본을 정리합니다. |
| FR-029 | 상담 할당량 | 상담원별 일·주·월 목표 수정, 기간별 배정·처리·잔여 건수 조회. 한국 시간 기준으로 집계하며 한 주는 월요일에 시작합니다. |
| FR-030 | 변경 이력 | 계정·할당량·상담 기록·녹취의 변경과 삭제, 재배정 전후 담당자를 작업한 관리자와 시각으로 확인합니다. |

관리자는 보관 기한 내 전화 응대 기록과 종결 메모도 직접 수정할 수 있습니다. 상담원이 재배정되면 녹취 재생 권한은 현재 담당자에게 넘어갑니다.

‘상담 기록 → 채팅·전화 상담 내역’의 각 행에서 ‘수정’을 누르면 해당 상담을 편집할 수 있습니다. ‘삭제’를 확인하면 티켓이 관리자 목록과 상담원 대기열에서 사라지고 고객 채팅과 녹취 재생도 차단됩니다. 상담 내용은 즉시 삭제되고 녹취 원본은 파기 작업이 정리합니다. 삭제한 관리자와 시각은 변경 이력에 남습니다.

‘상담 기록 → 기록 추가’에서 기존 티켓을 골라 직원용 기록을 작성합니다. 관리자는 목록에서 수정·삭제할 수 있고 현재 담당 상담원은 상담 상세에서 읽을 수 있습니다. 고객 채팅에는 표시되지 않습니다. 추가·수정·삭제 시각과 작업자는 변경 이력에 남으며, 기록 본문은 티켓의 상담 내용과 같은 기한에 파기됩니다.

## 거래소 연동

거래소 서버가 보낼 HTTP 헤더는 `Authorization: Bearer <PARTNER_TOKEN>`입니다. 토큰은 로컬 `.env`에 있습니다.

| 요청 | 용도 |
|---|---|
| `POST /cases` | `support_subject_ref`(고객 UUID), `intake_request_id`(접수 UUID), `channel`로 미배정 티켓을 생성합니다. 채널은 `WEB` 또는 `PHONE`이며, 생략하면 `WEB`입니다. 같은 요청을 재시도하면 같은 `case_id`를 반환합니다. 같은 접수 UUID에 고객이나 채널이 다르면 HTTP 409이며, 삭제된 티켓의 접수 UUID는 HTTP 410입니다. |
| `GET /cases?support_subject_ref=…&channel=PHONE` | 해당 고객의 요청 채널 티켓과 상태를 조회합니다. 채널을 생략하면 웹채팅 목록을 반환합니다. |
| `POST /cases/{case_id}/entry-codes` | 웹채팅 티켓에 `support_subject_ref`를 보내 일회용 `entry_code`를 발급받습니다. |

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

아래 명령은 검증 전용 프로젝트에 합성 티켓을 생성해 외부 접수, 동시 수락, 관리자 수정·재배정, 녹취와 종결을 검증합니다.

```powershell
$env:PORTAL_PORT = '18443'
docker compose -p mk-check up -d --build --wait
docker compose -p mk-check cp nginx:/etc/nginx/local-tls/cert.pem .local/check-cert.pem
python tests/smoke.py --port 18443 --cert .local/check-cert.pem
docker compose -p mk-check down
Remove-Item Env:PORTAL_PORT
```
