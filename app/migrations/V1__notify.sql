CREATE TABLE IF NOT EXISTS notify_channels (
  id CHAR(36) NOT NULL,
  user_id CHAR(36) NOT NULL,
  channel VARCHAR(16) NOT NULL,
  destination_ciphertext VARBINARY(512) NULL,
  destination_fingerprint BINARY(32) NULL,
  verified_at DATETIME(3) NULL,
  consent_at DATETIME(3) NULL,
  disconnected_at DATETIME(3) NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_notify_user_channel (user_id, channel),
  UNIQUE KEY uq_notify_destination (destination_fingerprint),
  KEY idx_notify_channel_user (channel, user_id, disconnected_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS telegram_challenges (
  id CHAR(36) NOT NULL,
  channel_id CHAR(36) NOT NULL,
  user_id CHAR(36) NOT NULL,
  token_hash BINARY(32) NOT NULL,
  expires_at DATETIME(3) NOT NULL,
  used_at DATETIME(3) NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_telegram_challenge_hash (token_hash),
  KEY idx_telegram_challenge_expiry (expires_at, used_at),
  CONSTRAINT fk_telegram_challenge_channel FOREIGN KEY (channel_id) REFERENCES notify_channels (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS notify_preferences (
  user_id CHAR(36) NOT NULL,
  quiet_start TIME NULL,
  quiet_end TIME NULL,
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS notification_history (
  id CHAR(36) NOT NULL,
  dispatch_id CHAR(36) NOT NULL,
  user_id CHAR(36) NOT NULL,
  channel_id CHAR(36) NULL,
  channel VARCHAR(16) NOT NULL,
  schedule_id CHAR(36) NULL,
  status ENUM('pending','processing','sent','failed','skipped','cancelled','unknown') NOT NULL,
  reason_code VARCHAR(40) NULL,
  provider_receipt_id VARCHAR(128) NULL,
  attempt_count INT NOT NULL DEFAULT 0,
  scheduled_at DATETIME(3) NOT NULL,
  is_test TINYINT(1) NOT NULL DEFAULT 0,
  processing_attempt INT NULL,
  processing_started_at DATETIME(3) NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_notify_dispatch (dispatch_id),
  KEY idx_notify_history_user_time (user_id, created_at),
  KEY idx_notify_history_retention (created_at),
  KEY idx_notify_test_rate (user_id, channel_id, is_test, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS notification_attempts (
  dispatch_id CHAR(36) NOT NULL,
  attempt INT NOT NULL,
  status ENUM('accepted','retryable_failure','permanent_failure','unknown','cancelled','skipped') NOT NULL,
  reason_code VARCHAR(40) NULL,
  provider_receipt_id VARCHAR(128) NULL,
  occurred_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (dispatch_id, attempt),
  KEY idx_notify_attempts_retention (occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS telegram_updates (
  update_id BIGINT NOT NULL,
  received_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (update_id),
  KEY idx_telegram_update_retention (received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS notify_deleted_users (
  user_fingerprint BINARY(32) NOT NULL,
  processed_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (user_fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Stable per-user lock also serializes late events against account deletion.
CREATE TABLE IF NOT EXISTS notify_user_state (
  user_fingerprint BINARY(32) NOT NULL,
  deleted_at DATETIME(3) NULL,
  PRIMARY KEY (user_fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS notify_account_events (
  event_fingerprint BINARY(32) NOT NULL,
  user_fingerprint BINARY(32) NOT NULL,
  processed_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (event_fingerprint),
  KEY idx_notify_account_event_user (user_fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
