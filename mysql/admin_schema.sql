CREATE OR REPLACE VIEW visible_case AS
    SELECT * FROM support_case WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS voice_session (
    voice_id CHAR(36) PRIMARY KEY,
    owner_hash BINARY(32) NOT NULL,
    subject_ref CHAR(36) NOT NULL,
    FOREIGN KEY (owner_hash) REFERENCES customer_session(token_hash) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS agent_quota (
    staff_id CHAR(36) PRIMARY KEY,
    daily INT UNSIGNED,
    weekly INT UNSIGNED,
    monthly INT UNSIGNED,
    FOREIGN KEY (staff_id) REFERENCES staff_account(staff_id)
) ENGINE=InnoDB;

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
