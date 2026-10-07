-- Twilio basic-call verification, correlation, transfer-leg, and cost state.

ALTER TABLE user_twilio_credentials ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'unverified';
ALTER TABLE user_twilio_credentials ADD COLUMN verified_at TEXT;
ALTER TABLE user_twilio_credentials ADD COLUMN verification_error TEXT;

ALTER TABLE audios ADD COLUMN content_type TEXT;

CREATE TABLE IF NOT EXISTS caller_id_provider_status (
  caller_id_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  provider TEXT NOT NULL,
  provider_sid TEXT,
  status TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  error TEXT,
  PRIMARY KEY (caller_id_id, provider),
  FOREIGN KEY (caller_id_id) REFERENCES caller_ids(id) ON DELETE CASCADE,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_caller_id_provider_status_user
  ON caller_id_provider_status(user_id, provider, status);

ALTER TABLE campaign_numbers ADD COLUMN dispatch_attempt_id TEXT;
ALTER TABLE campaign_numbers ADD COLUMN provider_sequence INTEGER NOT NULL DEFAULT -1;
ALTER TABLE campaign_numbers ADD COLUMN provider_terminal_at TEXT;
ALTER TABLE campaign_numbers ADD COLUMN transfer_call_sid TEXT;
ALTER TABLE campaign_numbers ADD COLUMN transfer_status TEXT;
ALTER TABLE campaign_numbers ADD COLUMN transfer_sequence INTEGER NOT NULL DEFAULT -1;
ALTER TABLE campaign_numbers ADD COLUMN transfer_duration_seconds INTEGER;
ALTER TABLE campaign_numbers ADD COLUMN transfer_connected_at TEXT;
ALTER TABLE campaign_numbers ADD COLUMN pressed_1_at TEXT;
ALTER TABLE campaign_numbers ADD COLUMN outcome_reason TEXT;
ALTER TABLE campaign_numbers ADD COLUMN cost_reconcile_status TEXT NOT NULL DEFAULT 'not_started';
ALTER TABLE campaign_numbers ADD COLUMN cost_reconcile_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE campaign_numbers ADD COLUMN cost_next_retry_at TEXT;
ALTER TABLE campaign_numbers ADD COLUMN cost_reconciled_at TEXT;

CREATE INDEX IF NOT EXISTS ix_campaign_numbers_attempt
  ON campaign_numbers(id, dispatch_attempt_id);
CREATE INDEX IF NOT EXISTS ix_campaign_numbers_cost_reconcile
  ON campaign_numbers(campaign_id, cost_reconcile_status, cost_next_retry_at);
