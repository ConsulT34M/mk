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

CREATE TABLE IF NOT EXISTS staff_notice (
    notice_id CHAR(36) PRIMARY KEY,
    author_id CHAR(36) NOT NULL,
    title VARCHAR(100) NOT NULL,
    body TEXT NOT NULL,
    published_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (author_id) REFERENCES staff_account(staff_id),
    INDEX (published_at, notice_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS voice_policy (
    policy_id TINYINT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    instructions VARCHAR(600) NOT NULL DEFAULT '',
    offer_handoff BOOLEAN NOT NULL DEFAULT TRUE,
    CHECK (policy_id=1)
) ENGINE=InnoDB;
INSERT IGNORE INTO voice_policy (policy_id) VALUES (1);

CREATE TABLE IF NOT EXISTS ai_call_summary (
    recording_id CHAR(36) PRIMARY KEY,
    state ENUM('QUEUED','PROCESSING','READY','FAILED') NOT NULL DEFAULT 'QUEUED',
    summary TEXT,
    error_message VARCHAR(160),
    FOREIGN KEY (recording_id) REFERENCES recording(recording_id) ON DELETE CASCADE,
    INDEX (state)
) ENGINE=InnoDB;

CREATE OR REPLACE VIEW visible_case AS
    SELECT * FROM support_case WHERE deleted_at IS NULL;
