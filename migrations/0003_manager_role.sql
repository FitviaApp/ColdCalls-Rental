-- Manager role: staff who run day-to-day operations (users, prices, paid days) without platform-wide admin.

ALTER TABLE users ADD COLUMN is_manager INTEGER NOT NULL DEFAULT 0;
