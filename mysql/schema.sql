-- 상담원·관리자의 로그인 계정과 권한
CREATE TABLE staff_account (
    staff_id CHAR(36) PRIMARY KEY,
    login_id VARCHAR(80) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('ADMIN','AGENT') NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
) ENGINE=InnoDB;

-- 직원 로그인 세션과 만료 시각
CREATE TABLE staff_session (
    token_hash BINARY(32) PRIMARY KEY,
    staff_id CHAR(36) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    FOREIGN KEY (staff_id) REFERENCES staff_account(staff_id),
    INDEX (expires_at)
) ENGINE=InnoDB;

-- 상담 한 건의 접수·배정·종결 정보
CREATE TABLE support_case (
    case_id CHAR(36) PRIMARY KEY,
    channel ENUM('WEB','PHONE') NOT NULL,
    support_subject_ref CHAR(36),
    intake_request_id CHAR(36) UNIQUE,
    current_agent_id CHAR(36),
    opened_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    closed_at DATETIME(6),
    deleted_at DATETIME(6),
    result_note TEXT,
    content_expires_at DATETIME(6),
    content_purged_at DATETIME(6),
    FOREIGN KEY (current_agent_id) REFERENCES staff_account(staff_id),
    INDEX (support_subject_ref, opened_at),
    INDEX (content_expires_at, content_purged_at)
) ENGINE=InnoDB;

-- 상담 담당자 배정·변경 이력
CREATE TABLE assignment_history (
    assignment_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    case_id CHAR(36) NOT NULL,
    previous_agent_id CHAR(36),
    new_agent_id CHAR(36),
    changed_by CHAR(36) NOT NULL,
    changed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (case_id) REFERENCES support_case(case_id),
    FOREIGN KEY (previous_agent_id) REFERENCES staff_account(staff_id),
    FOREIGN KEY (new_agent_id) REFERENCES staff_account(staff_id),
    FOREIGN KEY (changed_by) REFERENCES staff_account(staff_id)
) ENGINE=InnoDB;

-- 채팅 입장용 일회용 코드의 해시와 사용 시각
CREATE TABLE entry_code (
    code_hash BINARY(32) PRIMARY KEY,
    case_id CHAR(36) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    used_at DATETIME(6),
    FOREIGN KEY (case_id) REFERENCES support_case(case_id),
    INDEX (expires_at)
) ENGINE=InnoDB;

-- 문의 방문자와 채팅 고객의 세션
CREATE TABLE customer_session (
    token_hash BINARY(32) PRIMARY KEY,
    case_id CHAR(36),
    expires_at DATETIME(6) NOT NULL,
    FOREIGN KEY (case_id) REFERENCES support_case(case_id),
    INDEX (expires_at)
) ENGINE=InnoDB;

-- 상담별 채팅 메시지와 재전송 확인용 ID
CREATE TABLE chat_message (
    message_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    case_id CHAR(36) NOT NULL,
    client_message_id CHAR(36) NOT NULL,
    author_staff_id CHAR(36),
    body TEXT NOT NULL,
    sent_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (case_id) REFERENCES support_case(case_id),
    FOREIGN KEY (author_staff_id) REFERENCES staff_account(staff_id),
    UNIQUE (case_id, client_message_id)
) ENGINE=InnoDB;

-- 전화 응대 내용과 녹취 동의 상태
CREATE TABLE phone_response (
    case_id CHAR(36) PRIMARY KEY,
    call_at DATETIME(6),
    consent_state ENUM('UNASKED','AGREED','REFUSED') NOT NULL DEFAULT 'UNASKED',
    consent_by CHAR(36),
    consent_at DATETIME(6),
    inquiry TEXT,
    guidance TEXT,
    followup_required BOOLEAN,
    FOREIGN KEY (case_id) REFERENCES support_case(case_id),
    FOREIGN KEY (consent_by) REFERENCES staff_account(staff_id)
) ENGINE=InnoDB;

-- 녹취 파일의 저장 위치와 상태. expires_at은 수신 중인 AI 통화의 유휴 기한이다.
CREATE TABLE recording (
    recording_id CHAR(36) PRIMARY KEY,
    case_id CHAR(36),
    upload_request_id CHAR(36) NOT NULL,
    uploaded_by CHAR(36),
    label VARCHAR(120),
    state ENUM('PENDING','READY','FAILED','DELETE_PENDING','DESTROYED') NOT NULL,
    object_key VARCHAR(80) UNIQUE,
    storage_bucket VARCHAR(63),
    size_bytes INT UNSIGNED,
    sha256 BINARY(32),
    expires_at DATETIME(6),
    call_started_at DATETIME(6),
    stored_at DATETIME(6),
    destroyed_at DATETIME(6),
    FOREIGN KEY (case_id) REFERENCES support_case(case_id),
    FOREIGN KEY (uploaded_by) REFERENCES staff_account(staff_id),
    UNIQUE (case_id, upload_request_id),
    INDEX (expires_at, state)
) ENGINE=InnoDB;

-- 상담 조회·녹취 재생 등 작업의 주체와 결과
CREATE TABLE audit_event (
    event_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    action VARCHAR(32) NOT NULL,
    actor VARCHAR(64) NOT NULL,
    case_id CHAR(36),
    recording_id CHAR(36),
    result VARCHAR(48) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX (case_id, created_at)
) ENGINE=InnoDB;
