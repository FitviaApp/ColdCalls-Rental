-- Manager role: staff accounts that manage users and paid days, with audit trail.

ALTER TABLE users ADD COLUMN is_manager INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN created_by INTEGER REFERENCES users(id);

CREATE TABLE IF NOT EXISTS paid_day_grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  granted_by INTEGER NOT NULL,
  days INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_paid_day_grants_user ON paid_day_grants(user_id);
CREATE INDEX IF NOT EXISTS ix_paid_day_grants_granted_by ON paid_day_grants(granted_by, created_at);
