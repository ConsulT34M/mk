-- 직원 소속 팀 목록
CREATE TABLE IF NOT EXISTS staff_team (
    team_id CHAR(36) PRIMARY KEY,
    name VARCHAR(40) NOT NULL UNIQUE
) ENGINE=InnoDB;

ALTER TABLE staff_account ADD COLUMN display_name VARCHAR(60) NULL;
ALTER TABLE staff_account ADD COLUMN team_id CHAR(36) NULL,
    ADD CONSTRAINT fk_staff_team FOREIGN KEY (team_id) REFERENCES staff_team(team_id);

ALTER TABLE support_case ADD COLUMN inquiry_type ENUM('ACCOUNT','FUNDS','TRADE','GUIDE') NULL;
ALTER TABLE support_case ADD COLUMN outcome ENUM('RESOLVED','FOLLOWUP') NULL;
ALTER TABLE support_case ADD COLUMN call_direction ENUM('INBOUND','OUTBOUND') NULL;
ALTER TABLE support_case ADD COLUMN talk_seconds INT UNSIGNED NULL;

-- 직원 게시판. 삭제한 글은 재전송으로 되살아나지 않도록 식별자를 남기고 제목과 본문을 비운다.
CREATE TABLE IF NOT EXISTS staff_board_post (
    post_id CHAR(36) PRIMARY KEY,
    author_id CHAR(36) NOT NULL,
    title VARCHAR(100),
    body TEXT,
    revision INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (author_id) REFERENCES staff_account(staff_id),
    INDEX (created_at, post_id)
) ENGINE=InnoDB;

-- 직원 공지의 작성자와 게시 내용
CREATE TABLE IF NOT EXISTS staff_notice (
    notice_id CHAR(36) PRIMARY KEY,
    author_id CHAR(36) NOT NULL,
    title VARCHAR(100) NOT NULL,
    body TEXT NOT NULL,
    published_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (author_id) REFERENCES staff_account(staff_id),
    INDEX (published_at, notice_id)
) ENGINE=InnoDB;

-- AI 통화 사용 여부, 응대 지침과 상담원 인계 설정
CREATE TABLE IF NOT EXISTS voice_policy (
    policy_id TINYINT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    instructions VARCHAR(600) NOT NULL DEFAULT '',
    offer_handoff BOOLEAN NOT NULL DEFAULT TRUE,
    CHECK (policy_id=1)
) ENGINE=InnoDB;
ALTER TABLE voice_policy ADD COLUMN greeting VARCHAR(200) NOT NULL DEFAULT '안녕하십니까. 고객센터 AI 상담원입니다. 어떤 일로 연락 주셨나요?';
ALTER TABLE voice_policy ADD COLUMN ringback_enabled BOOLEAN NOT NULL DEFAULT TRUE;
INSERT IGNORE INTO voice_policy (policy_id) VALUES (1);

-- RKX AI 채팅 설정과 대화 내역
CREATE TABLE IF NOT EXISTS ai_chat_policy (
    policy_id TINYINT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    greeting VARCHAR(200) NOT NULL DEFAULT '안녕하세요. RKX AI 상담입니다. 무엇을 도와드릴까요?',
    reply_style ENUM('brief','steps') NOT NULL DEFAULT 'brief',
    CHECK (policy_id=1)
) ENGINE=InnoDB;
INSERT IGNORE INTO ai_chat_policy (policy_id) VALUES (1);

CREATE TABLE IF NOT EXISTS ai_chat_session (
    chat_id CHAR(36) PRIMARY KEY,
    subject_ref CHAR(36) NOT NULL,
    greeting VARCHAR(200) NOT NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX (subject_ref, updated_at), INDEX (updated_at)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS ai_chat_turn (
    turn_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    chat_id CHAR(36) NOT NULL,
    request_id CHAR(36) NOT NULL,
    question VARCHAR(2000) NOT NULL,
    answer TEXT NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (chat_id) REFERENCES ai_chat_session(chat_id) ON DELETE CASCADE,
    UNIQUE (chat_id, request_id), INDEX (chat_id, turn_id)
) ENGINE=InnoDB;

-- 녹취 동의와 별도로 RKX 전화 상담의 연결 상태만 기록한다.
CREATE TABLE IF NOT EXISTS rkx_voice_call (
    voice_id CHAR(36) PRIMARY KEY,
    subject_ref CHAR(36) NOT NULL,
    requested_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    connected_at DATETIME(6) NULL, ended_at DATETIME(6) NULL,
    INDEX (requested_at)
) ENGINE=InnoDB;

-- AI 통화 녹취의 요약 결과와 처리 상태
CREATE TABLE IF NOT EXISTS ai_call_summary (
    recording_id CHAR(36) PRIMARY KEY,
    state ENUM('QUEUED','PROCESSING','READY','FAILED') NOT NULL DEFAULT 'QUEUED',
    summary TEXT,
    error_message VARCHAR(160),
    FOREIGN KEY (recording_id) REFERENCES recording(recording_id) ON DELETE CASCADE,
    INDEX (state)
) ENGINE=InnoDB;

-- 삭제 표시된 상담을 제외한 조회용 뷰
CREATE OR REPLACE VIEW visible_case AS
    SELECT * FROM support_case WHERE deleted_at IS NULL;
