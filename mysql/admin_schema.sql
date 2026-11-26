-- 삭제 표시된 상담을 제외한 조회용 뷰
CREATE OR REPLACE VIEW visible_case AS
    SELECT * FROM support_case WHERE deleted_at IS NULL;

-- 방문자 세션과 AI 통화의 연결 정보
CREATE TABLE IF NOT EXISTS voice_session (
    voice_id CHAR(36) PRIMARY KEY,
    owner_hash BINARY(32) NOT NULL,
    subject_ref CHAR(36) NOT NULL,
    FOREIGN KEY (owner_hash) REFERENCES customer_session(token_hash) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 상담원의 일·주·월별 업무 목표
CREATE TABLE IF NOT EXISTS agent_quota (
    staff_id CHAR(36) PRIMARY KEY,
    daily INT UNSIGNED DEFAULT 10,
    weekly INT UNSIGNED DEFAULT 50,
    monthly INT UNSIGNED DEFAULT 200,
    FOREIGN KEY (staff_id) REFERENCES staff_account(staff_id)
) ENGINE=InnoDB;

-- 관리자의 변경 작업과 대상
CREATE TABLE IF NOT EXISTS admin_change (
    change_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    actor_id CHAR(36) NOT NULL,
    action VARCHAR(40) NOT NULL,
    target VARCHAR(255) NOT NULL,
    case_id CHAR(36),
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (actor_id) REFERENCES staff_account(staff_id),
    FOREIGN KEY (case_id) REFERENCES support_case(case_id)
) ENGINE=InnoDB;

-- 상담별로 직원이 작성한 내부 메모
CREATE TABLE IF NOT EXISTS staff_note (
    note_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    case_id CHAR(36) NOT NULL,
    request_id CHAR(36) NOT NULL,
    created_by CHAR(36) NOT NULL,
    body TEXT,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE (case_id, request_id),
    FOREIGN KEY (case_id) REFERENCES support_case(case_id),
    FOREIGN KEY (created_by) REFERENCES staff_account(staff_id)
) ENGINE=InnoDB;
