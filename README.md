# AI TM 상담 데스크

거래소 고객의 웹채팅과 모의 전화 상담을 처리하는 수탁사 직원 포털입니다. Docker Compose에서 Nginx, Flask와 MySQL 8.4를 실행합니다.

## 폴더 구성

- `app/`에 Flask 코드가 있습니다. 화면 템플릿은 `app/templates/`, 스타일·스크립트·서비스 로고는 `app/static/`, 상담 데스크 시안은 `app/static/prototype/`에 있습니다.
- `app/queries/`는 상담(`cases`), 채팅(`chat`), 직원(`staff`), 전화·AI 통화(`voice`), 녹취(`recordings`), 통계(`reports`)별 SQL을 관리합니다.
- `mysql/`에는 데이터베이스 초기화 파일(`schema.sql`)과 관리자·상담 데스크 마이그레이션 SQL이 있습니다. `nginx/`에는 HTTPS 설정이 있습니다.
- `tests/`에는 상담 흐름 통합 테스트가 있습니다. 로컬 디자인 원본은 `design/`에 보관합니다.

## 실행

Docker Desktop의 Linux 컨테이너 엔진과 Python 3에서 프로젝트 폴더를 열고 실행합니다.

```powershell
python setup_demo.py
docker compose up -d --build --wait
```

직원 포털은 [직원 로그인](https://localhost:8443), 고객용 문의 창은 [고객센터](https://localhost:8443/inquiry)에서 엽니다. 첫 실행 시 Nginx가 로컬용 자체 서명 인증서를 생성합니다. `docker compose stop`으로 중지하고 `docker compose start`로 다시 시작합니다.

## 로컬 시연 계정

| 역할 | 아이디 | 비밀번호 | 로그인 후 화면 |
|---|---|---|---|
| 관리자 | `admin` | `admin` | [관리자 대시보드](https://localhost:8443/admin) |
| 상담원 | `test` | `test` | [상담원 포털](https://localhost:8443/portal) |

새 로컬 환경의 기본값입니다. 새 DB를 만들 때 `.env`에 지정한 `DEMO_ADMIN_PASSWORD`를 관리자 초기 비밀번호로 사용하며, 기존 DB의 계정과 비밀번호는 재시작 후에도 유지됩니다. 재배정 시연용 상담원 `agent1`, `agent2`의 비밀번호는 `.env`의 `DEMO_AGENT1_PASSWORD`, `DEMO_AGENT2_PASSWORD`에 있습니다.

## 티켓 접수와 담당자 변경

웹채팅을 시연하려면 고객 탭에서 `/inquiry`의 ‘문의하기’를 누르고 메시지를 남깁니다. 버튼을 처음 누를 때 가상 고객의 대기 티켓이 발행됩니다. 상담원 탭에서 `test`로 로그인해 [대시보드](https://localhost:8443/portal?tab=dashboard)의 같은 번호 옆 ‘상담 받기’를 누르면 서로 답변을 주고받을 수 있습니다. 같은 브라우저에서 고객 세션이 유효한 동안 창을 닫고 다시 열어도 대화가 이어집니다. 종결 후에는 이전 대화를 읽거나 ‘새 문의하기’로 다음 티켓을 열 수 있습니다.

1. 서로 다른 브라우저 프로필에서 관리자와 상담원으로 로그인합니다.
2. 외부 서버가 `POST /cases`로 웹채팅 또는 전화 요청을 보내면 미배정 티켓이 생성됩니다. 아래 명령으로 외부 요청을 시연할 수 있습니다. 첫 번째 명령은 고객 채팅 입장 코드를, 두 번째 명령은 새 전화 티켓 번호를 출력합니다.

   ```powershell
   docker compose exec -T app flask demo-entry --customer 1 --new
   docker compose exec -T app flask demo-entry --customer 1 --channel PHONE --new
   ```

3. 상담원 대시보드에서 ‘상담 받기’를 먼저 누른 상담원이 티켓을 가져갑니다. 고객은 거래소 채팅이나 고객센터의 문의 창에서 상담에 참여합니다. 전화 상담의 담당자는 응대 내용과 녹취 동의 여부를 기록하고, 동의한 건에 `.local/sample.wav`를 등록할 수 있습니다.
4. 수락한 티켓은 관리자 대시보드에 ‘진행 중’으로 표시됩니다. 관리자는 이때 ‘담당자 변경’으로 다른 활성 상담원에게 재배정할 수 있습니다. 이후 새 담당자가 이어서 응대하고 처리 결과를 입력해 종결합니다.

## RKX 상담 채널

RKX의 상담 버튼에서 시작한 대화는 MK 관리자 메뉴에서 확인합니다. 상담원 메시지는 ‘상담 운영 → 채팅’, AI와 주고받은 문자는 ‘AI → AI 채팅’, 브라우저 전화 상담은 ‘AI → AI 통화’에 표시됩니다. 전화 상담은 연결·종료 시각을 남기고 녹취와 요약에는 고객의 동의를 받습니다.

운영 대시보드의 ‘AI 상담 현황’은 문의 유형 분포와 같은 조회 기간·접수 채널을 사용합니다. 채팅은 첫 답변일, 통화는 시작일 기준으로 한 번씩 셉니다. 자동 해결률은 고객이 ‘해결됐어요’를 선택한 건을 해결 확인과 상담원 접수 건의 합으로 나눈 값입니다. 결과 미확인은 별도로 표시합니다. ‘상담원 전환 현황’에서는 실제 접수된 AI 상담을 채팅과 통화로 나누어 볼 수 있습니다. 과거 통화도 인계 티켓이 남아 있으면 전환에 포함합니다.

관리자의 ‘AI → AI 설정’에서 OpenAI API 키를 등록하거나 교체하고 채팅·음성 통화·녹취 요약 모델을 각각 선택합니다. ‘모델 조회’를 누르면 입력한 키(비어 있으면 현재 설정된 키)로 채팅 모델 목록을 불러옵니다. 모델을 선택한 뒤 ‘OpenAI 설정 저장’을 누릅니다. 키 입력을 비우고 저장하면 기존 키를 유지합니다. 변경은 다음 채팅 답변, 새 통화, 새 요약 작업부터 적용됩니다. 채팅·음성 운영 여부와 인사말도 같은 화면에서 설정합니다.

화면에 등록한 키는 DB에 암호화해 저장하며 끝 4자리만 표시합니다. 복원이나 서버 이전 시 DB와 함께 서비스 전용 `RECORDING_DIR/.openai-settings.key`를 백업합니다. 화면에 등록한 키가 없으면 `OPENAI_API_KEY`를 사용합니다. 모델에서 ‘서버 기본값’을 선택하면 `OPENAI_CHAT_MODEL`, `OPENAI_REALTIME_MODEL`, `OPENAI_CALL_SUMMARY_MODEL`을 사용합니다.

## 브라우저 AI 음성 상담

운영 대시보드는 AI 채팅과 통화의 토큰 사용량을 합산하고, 각 AI 탭에서는 해당 채널의 사용량과 상담 내역을 함께 보여 줍니다. 통화에는 실시간 모델 응답과 녹취 요약이 포함됩니다. 한국 시간 기준으로 수집된 응답의 입력·출력을 합산하며, 캐시 적중률은 전체 입력 중 캐시에서 읽은 토큰의 비율입니다. 집계 시작 전 상담은 기존 내역에서 확인할 수 있습니다.

1. 관리자 ‘AI 설정’에서 OpenAI 키와 음성 통화 모델을 확인합니다. 초기 통화 모델은 `gpt-realtime-2.1-mini`입니다.
2. 고객센터에서 ‘AI 음성 상담 → 음성 상담 시작’을 누르고 마이크 사용을 허용합니다. MK가 WebRTC 연결 정보를 교환하면 브라우저가 음성을 주고받습니다. MK 서버는 OpenAI에 별도 WebSocket 연결을 열어 각 응답의 사용량을 수신합니다. [로컬 고객센터](https://localhost:8443/inquiry) · [OpenAI WebRTC 가이드](https://developers.openai.com/api/docs/guides/voice-webrtc?voice-api=realtime)
3. “상담원 연결해 주세요”라고 말하면 AI의 `request_agent` 도구 호출을 브라우저가 Flask로 전달합니다. Flask는 고객 세션을 확인하고 통화당 한 건의 미배정 전화 티켓을 발행합니다. 고객에게 표시된 번호를 기존 상담원 대시보드에서 확인할 수 있습니다. 이 시연의 인계 범위는 전화 티켓 접수까지입니다. 티켓의 ‘통화 일시’에는 고객과 AI가 연결된 시각이 자동 입력됩니다. 별도 전화기로 받은 일반 전화는 담당자가 통화 일시를 입력합니다.
4. ‘통화 녹취와 AI 요약에 동의합니다’를 선택하면 WAV와 고객 요청 한 줄 요약을 저장합니다. 관리자는 ‘상담 운영 → AI 통화’, 담당 상담원은 통화 상세 또는 ‘통화 내역·녹취’에서 확인합니다. 관리자는 기존 녹취의 ‘요약 생성’을 눌러 요약을 만들 수 있습니다. 녹취와 요약은 관리자 삭제 또는 녹취 동의 철회 시까지 보관합니다. [로컬 AI 통화](https://localhost:8443/admin?tab=ai-calls)
5. 녹취 선택은 통화마다 기본 해제됩니다. 동의한 통화에서 브라우저가 비정상 종료되면 서버가 받은 부분까지 복구합니다. 마지막 전송으로부터 2분 후 시작되는 정리 주기에 반영됩니다.

요약 작업은 녹취가 저장된 뒤 관리자 화면에 설정한 키와 녹취 요약 모델로 실행됩니다. 초기 요약 모델은 `gpt-audio-1.5`입니다. [OpenAI 음성 입력 가이드](https://developers.openai.com/api/docs/guides/audio-chat-completions)

## 관리자 페이지 (F07)

`/admin` 대시보드는 오늘 시간대별 통화·채팅 접수와 선택한 일·월의 문의 유형 분포를 보여줍니다. 유형을 입력하지 않은 티켓은 미분류로 집계합니다. 통화와 채팅 메뉴에서는 오늘 종결 건수, 현재 대기 건수, 최장 대기 시간과 오늘 최초 수락한 상담의 평균 응답 시간을 확인합니다.

| 요구사항 | 메뉴 | 관리자 작업 |
|---|---|---|
| FR-026 | 계정 관리 | 상담원을 추가하고 행의 ‘수정’에서 이름·아이디·비밀번호와 할당량을 변경하거나 계정을 삭제(비활성화)·복구합니다. 비활성화하면 세션이 종료되고 진행 중인 티켓은 대기열로 돌아갑니다. |
| FR-027 | 상담 기록 → 웹채팅 | 대화 조회, 개별 메시지 수정·삭제, 상담 기록 전체 삭제. 변경 사항은 고객과 상담원의 대화에도 반영됩니다. |
| FR-028 | 상담 기록 → 녹취 | 파일 상태·크기 조회, 표시명 수정, 녹취 삭제. 삭제 요청 시 재생이 즉시 차단되고 파기 작업이 원본을 정리합니다. |
| FR-029 | 계정 관리 | 일·주·월 목표를 함께 설정하고 기간별 배정·처리·잔여 건수를 조회합니다. 한국 시간 기준으로 집계하며 한 주는 월요일에 시작합니다. |
| FR-030 | 변경 이력 | 계정·할당량·상담 기록·녹취의 변경과 삭제, 재배정, 조직 수정, 공지 게시, AI 설정 변경의 작업자와 시각을 확인합니다. |
| FR-038 | 조직 | 기존 상담원은 최초 적용 시 ‘고객 상담팀’으로 이동하고, 신규 상담원은 기본 팀에 자동 배정됩니다. 관리자는 조직도에서 팀원과 일·주·월 목표를 관리합니다. 팀 목표는 기간별로 팀원 목표를 합산하며 직접 입력한 값이 있으면 그 값을 사용합니다. 상담원 대시보드에는 개인·팀 배정량이 함께 표시됩니다. |
| FR-039 | 게시판 → 공지사항 | 관리자가 공지를 게시하면 상담원 대시보드에 최신 공지가 나타납니다. 상담원과 관리자는 게시판 상단에서 공지 목록과 본문을, 하단에서 일반 게시글을 확인합니다. |
| FR-040 | AI 설정 | 음성 상담 운영 여부, 연결 대기음 재생, 연결 후 첫 인사말, 추가 응대 지침과 상담원 연결 제안을 설정합니다. 저장한 값은 새 통화부터 적용됩니다. |

관리자는 보관 기한 내 전화 응대 기록과 종결 메모도 직접 수정할 수 있습니다. 상담원이 재배정되면 녹취 재생 권한은 현재 담당자에게 넘어갑니다.

‘상담 운영 → 채팅·통화’ 목록의 각 행에서 상담을 조회·수정·삭제합니다. 수정 화면에서는 문의 유형, 개별 메시지, 전화 응대 기록, 직원용 기록과 종결 결과를 정정하고 녹취 표시명을 바꿀 수 있습니다. ‘응대 기록 삭제’는 선택한 통화의 일시·구분·시간과 응대 내용을 지웁니다. 개별 녹취의 ‘삭제’는 재생을 차단하고 원본을 파기합니다. 목록의 ‘삭제’는 상담 티켓 전체에 적용되며 작업자와 시각은 변경 이력에 남습니다.

‘상담 기록 → 기록 추가’에서 기존 티켓을 골라 직원용 기록을 작성합니다. 관리자는 목록에서 수정·삭제할 수 있고 현재 담당 상담원은 상담 상세에서 읽을 수 있습니다. 고객 채팅에는 표시되지 않습니다. 추가·수정·삭제 시각과 작업자는 변경 이력에 남으며, 기록 본문은 티켓의 상담 내용과 같은 기한에 파기됩니다.

## 상담원 통계와 녹취

‘내 상담’에서 진행 중인 통화와 채팅을 각각 열 수 있습니다. 담당자는 문의 유형을 지정하고, 종결할 때 해결·후속 안내를 기록합니다. 종결한 건은 ‘내 종결 상담’에서 확인합니다. ‘통화 내역·녹취’에서는 통화일을 기준으로 담당 통화를 찾고 보관 중인 녹취를 재생합니다.

‘통계’에서는 한국 시간 기준으로 시작일과 종료일을 포함해 종결 건을 집계합니다. 개인 또는 현재 소속 팀의 처리 현황, 문의 유형, 처리 결과, 실제 수신·발신 통화 시간을 확인할 수 있습니다. 팀을 옮기면 과거 기간의 팀 합계도 현재 소속에 맞춰 바뀝니다. 통화 시간은 전화 응대에 입력한 초를 사용하고 미입력과 0초를 구분합니다.

## DB 변경 스크립트

`mysql/desk_schema.sql`은 팀·공지·AI 설정 테이블과 계정·상담 통계 컬럼을 추가하고 조회 뷰를 갱신합니다. 적용 명령은 `flask migrate-desk`입니다. 기존 컬럼을 확인하므로 중단 후 다시 실행할 수 있습니다. 새 앱 이미지의 초기화(`flask init-admin`)도 같은 마이그레이션을 호출합니다.

## 거래소 연동

거래소 서버가 보낼 HTTP 헤더는 `Authorization: Bearer <PARTNER_TOKEN>`입니다. 토큰은 로컬 `.env`에 있습니다.

| 요청 | 용도 |
|---|---|
| `POST /cases` | `support_subject_ref`(고객 UUID), `intake_request_id`(접수 UUID), `channel`로 미배정 티켓을 생성합니다. 채널은 `WEB` 또는 `PHONE`이며, 생략하면 `WEB`입니다. 같은 요청을 재시도하면 같은 `case_id`를 반환합니다. 같은 접수 UUID에 고객이나 채널이 다르면 HTTP 409이며, 삭제된 티켓의 접수 UUID는 HTTP 410입니다. |
| `GET /cases?support_subject_ref=…&channel=PHONE` | 해당 고객의 요청 채널 티켓과 상태를 조회합니다. 채널을 생략하면 웹채팅 목록을 반환합니다. |
| `POST /cases/{case_id}/entry-codes` | 웹채팅 티켓에 `support_subject_ref`를 보내 일회용 `entry_code`를 발급받습니다. |

고객 브라우저에서 포털의 `/chat/enter`로 `entry_code`를 HTML 폼 POST하면 방 전용 쿠키를 발급하고 채팅으로 이동합니다. 포털과 같은 출처의 클라이언트는 `POST /chat/sessions`에 같은 필드를 JSON으로 보낼 수 있습니다. [Notion API 명세](https://app.notion.com/p/f160de8f474583b083d281a75e3f3f80)

## 저장과 만료

프로젝트 루트의 `.env`에서 새 녹취를 저장할 위치를 선택합니다. `S3_RECORDING_BUCKET`이 비어 있으면 Docker는 WAV를 `.local/recordings/`에 저장합니다. 호스트 폴더는 `RECORDINGS_PATH`로 지정합니다.

S3를 사용할 때는 아래 예시의 버킷명과 리전을 실제 값으로 바꿉니다.

```dotenv
S3_RECORDING_BUCKET=my-recording-bucket
AWS_REGION=ap-northeast-2
```

EC2의 IAM 역할에 해당 비공개 버킷의 `files/*` 객체에 대한 `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` 권한을 부여합니다. 로컬 Docker에서 임시 AWS 자격 증명을 쓰려면 `.env`에 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`을 함께 지정합니다.

변경한 앱은 `docker compose up -d --build`로 적용합니다. Docker 밖에서 실행 중인 기존 설치는 새 의존성을 설치하고 `flask --app app init-admin`을 실행한 뒤 웹·유지보수 서비스를 재시작합니다. 서비스 환경변수가 `.env`보다 우선합니다.

AI 통화 중인 `.part`는 `RECORDING_DIR`에 모으고, 완성된 WAV를 녹취별 저장소에 씁니다. MySQL은 `recording.object_key`에 `files/<녹취 UUID>.wav`를, `storage_bucket`에 저장한 버킷명을 기록합니다. 기존 로컬 녹취의 버킷명은 NULL입니다. 새 녹취의 저장 위치를 바꿔도 이전 녹취는 원래 위치에서 요약·재생·삭제합니다. 재생 권한과 삭제 상태는 Flask가 검사합니다.

| 로컬 설정 | 기본값 |
|---|---|
| `STAFF_SESSION_MINUTES` / `CHAT_SESSION_MINUTES` / `ENTRY_CODE_MINUTES` | 발급 후 각각 30분 / 60분 / 5분 |
| `WAV_MAX_MIB` | PCM WAV 최대 10 MiB |
| `CASE_RETENTION_MINUTES` | 상담 종결 후 응대 본문·메모 보관 시간, 기본 5분 |

설정을 바꾸려면 `.env`에 값을 추가하고 `docker compose up -d`를 실행합니다. 30초마다 삭제를 요청한 녹취와 보관 기한이 지난 상담 본문을 정리합니다. MySQL의 `assignment_history`와 `audit_event`에서 배정·조회·재생·파기 이력을 확인할 수 있습니다.

Git에는 소스와 실행 구성을 올립니다. 생성된 `.env`, 샘플 WAV, 로컬 인증서와 실제 녹취는 제외됩니다.

## 통합 검증

아래 명령은 검증 전용 프로젝트에 합성 티켓을 생성해 외부 접수, 동시 수락, 관리자 수정·재배정, 녹취와 종결을 검증합니다.

```powershell
$env:PORTAL_PORT = '18443'
$env:RECORDINGS_PATH = './.local/check-recordings'
docker compose -p mk-check up -d --build --wait
docker compose -p mk-check cp nginx:/etc/nginx/local-tls/cert.pem .local/check-cert.pem
python tests/smoke.py --port 18443 --cert .local/check-cert.pem
Get-Content -Raw -Encoding utf8 tests/recording_store.py | docker compose -p mk-check exec -T app python -B -
Get-Content -Raw -Encoding utf8 tests/voice.py | docker compose -p mk-check exec -T app python -B -
Get-Content -Raw -Encoding utf8 tests/desk_operations.py | docker compose -p mk-check exec -T app python -B -
docker compose -p mk-check down
Remove-Item Env:PORTAL_PORT
Remove-Item Env:RECORDINGS_PATH
```
