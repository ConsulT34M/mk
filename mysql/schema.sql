CREATE TABLE staff_account (
    staff_id CHAR(36) PRIMARY KEY,
    login_id VARCHAR(80) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('ADMIN','AGENT') NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
) ENGINE=InnoDB;

CREATE TABLE staff_session (
    token_hash BINARY(32) PRIMARY KEY,
    staff_id CHAR(36) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    FOREIGN KEY (staff_id) REFERENCES staff_account(staff_id),
    INDEX (expires_at)
) ENGINE=InnoDB;

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

CREATE TABLE entry_code (
    code_hash BINARY(32) PRIMARY KEY,
    case_id CHAR(36) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    used_at DATETIME(6),
    FOREIGN KEY (case_id) REFERENCES support_case(case_id),
    INDEX (expires_at)
) ENGINE=InnoDB;

CREATE TABLE customer_session (
    token_hash BINARY(32) PRIMARY KEY,
    case_id CHAR(36),
    expires_at DATETIME(6) NOT NULL,
    FOREIGN KEY (case_id) REFERENCES support_case(case_id),
    INDEX (expires_at)
) ENGINE=InnoDB;

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
    expires_at DATETIME(6) NOT NULL,
    stored_at DATETIME(6),
    destroyed_at DATETIME(6),
    FOREIGN KEY (case_id) REFERENCES support_case(case_id),
    FOREIGN KEY (uploaded_by) REFERENCES staff_account(staff_id),
    UNIQUE (case_id, upload_request_id),
    INDEX (expires_at, state)
) ENGINE=InnoDB;

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
