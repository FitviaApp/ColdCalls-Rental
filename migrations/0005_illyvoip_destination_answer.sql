-- The IllyVoIP control session can report answered before the PSTN destination
-- picks up. Call reports carry the real destination answer time.
ALTER TABLE campaign_numbers ADD COLUMN destination_answered_at TEXT;
