-- Initial schema (port of app/models.py SQLAlchemy models)

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  transfer_number TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS caller_ids (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  phone_number TEXT NOT NULL UNIQUE,
  country_code TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  is_active INTEGER NOT NULL DEFAULT 1,
  vox_callerid_id INTEGER,
  vox_verification_status TEXT NOT NULL DEFAULT 'not_started',
  vox_last_verification_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_caller_ids_user_id ON caller_ids(user_id);
CREATE INDEX IF NOT EXISTS ix_caller_ids_country_code ON caller_ids(country_code);

CREATE TABLE IF NOT EXISTS countries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  price_per_minute REAL NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audios (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  name TEXT NOT NULL,
  r2_key TEXT NOT NULL,
  r2_url TEXT NOT NULL,
  duration_seconds INTEGER,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_audios_user_id ON audios(user_id);

CREATE TABLE IF NOT EXISTS ai_agents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  system_prompt TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  language TEXT NOT NULL DEFAULT 'en',
  voice_id TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT 'gpt-realtime-2.1',
  temperature REAL NOT NULL DEFAULT 0.7,
  handoff_description TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_ai_agents_user_id ON ai_agents(user_id);

CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  caller_id_id INTEGER NOT NULL,
  country_id INTEGER NOT NULL,
  audio_id INTEGER,
  ai_agent_id INTEGER,
  campaign_mode TEXT NOT NULL DEFAULT 'audio',
  status TEXT NOT NULL DEFAULT 'draft',
  press_1_to_talk_with_agent INTEGER NOT NULL DEFAULT 0,
  voice_provider TEXT NOT NULL DEFAULT 'twilio',
  max_concurrent_calls INTEGER NOT NULL DEFAULT 1,
  total_numbers INTEGER NOT NULL DEFAULT 0,
  processed_numbers INTEGER NOT NULL DEFAULT 0,
  successful_calls INTEGER NOT NULL DEFAULT 0,
  failed_calls INTEGER NOT NULL DEFAULT 0,
  total_cost REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  started_at TEXT,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_campaigns_user_id ON campaigns(user_id);
CREATE INDEX IF NOT EXISTS ix_campaigns_status ON campaigns(status);
CREATE INDEX IF NOT EXISTS ix_campaigns_ai_agent_id ON campaigns(ai_agent_id);
CREATE INDEX IF NOT EXISTS ix_campaigns_campaign_mode ON campaigns(campaign_mode);
CREATE INDEX IF NOT EXISTS ix_campaigns_voice_provider ON campaigns(voice_provider);

CREATE TABLE IF NOT EXISTS campaign_numbers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL,
  phone_number TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  call_sid TEXT,
  duration_seconds INTEGER,
  cost REAL,
  answered_by TEXT,
  ai_turn_count INTEGER,
  ai_no_input_turns INTEGER,
  ai_last_user_input TEXT,
  ai_last_assistant_text TEXT,
  ai_handoff_reason TEXT,
  ai_runtime_error TEXT,
  dispatch_state TEXT NOT NULL DEFAULT 'idle',
  dispatch_attempts INTEGER NOT NULL DEFAULT 0,
  provider_status TEXT,
  claimed_at TEXT,
  next_action_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  processed_at TEXT,
  error_message TEXT,
  FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_campaign_numbers_status ON campaign_numbers(status);
CREATE INDEX IF NOT EXISTS ix_campaign_numbers_campaign_status ON campaign_numbers(campaign_id, status);
CREATE INDEX IF NOT EXISTS ix_campaign_numbers_dispatch ON campaign_numbers(campaign_id, dispatch_state, next_action_at);

CREATE TABLE IF NOT EXISTS provider_events (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  campaign_number_id INTEGER,
  received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_provider_events_number ON provider_events(campaign_number_id);

-- Per-user voice provider credentials (encrypted blobs)
CREATE TABLE IF NOT EXISTS user_twilio_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL UNIQUE,
  account_sid_encrypted TEXT NOT NULL,
  auth_token_encrypted TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS user_signalwire_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL UNIQUE,
  project_id_encrypted TEXT NOT NULL,
  api_token_encrypted TEXT NOT NULL,
  space_url_encrypted TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS user_telnyx_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL UNIQUE,
  api_key_encrypted TEXT NOT NULL,
  account_sid_encrypted TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS user_vonage_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL UNIQUE,
  application_id_encrypted TEXT NOT NULL,
  private_key_encrypted TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS user_voximplant_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL UNIQUE,
  account_id_encrypted TEXT NOT NULL,
  application_id_encrypted TEXT,
  service_account_email_encrypted TEXT NOT NULL,
  key_id_encrypted TEXT NOT NULL,
  private_key_encrypted TEXT NOT NULL,
  vox_app_id INTEGER,
  vox_rule_id INTEGER,
  vox_scenario_id INTEGER,
  provision_status TEXT NOT NULL DEFAULT 'pending',
  provision_error TEXT,
  provisioned_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_voximplant_provision_status ON user_voximplant_credentials(provision_status);

CREATE TABLE IF NOT EXISTS user_openai_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL UNIQUE,
  api_key_encrypted TEXT NOT NULL,
  organization_id_encrypted TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS user_elevenlabs_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL UNIQUE,
  api_key_encrypted TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS rental_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  duration_days INTEGER NOT NULL,
  price_usdt REAL NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS user_rentals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  plan_id INTEGER NOT NULL,
  starts_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  FOREIGN KEY (plan_id) REFERENCES rental_plans(id),
  FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS ix_user_rentals_user_status_expires ON user_rentals(user_id, status, expires_at);

CREATE TABLE IF NOT EXISTS rental_payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  plan_id INTEGER NOT NULL,
  rental_id INTEGER,
  tx_hash TEXT NOT NULL UNIQUE,
  amount_usdt REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  verified_at TEXT
);
