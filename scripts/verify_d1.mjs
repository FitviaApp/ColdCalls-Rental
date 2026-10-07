import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const target = process.argv.includes('--remote') ? '--remote' : '--local';
const sql = `
SELECT name FROM sqlite_master
 WHERE type='table' AND name IN ('users','rental_plans','campaigns','campaign_numbers','provider_events','caller_id_provider_status')
 ORDER BY name;
SELECT id,name,voice_provider,campaign_mode,status FROM campaigns ORDER BY id DESC LIMIT 5;
SELECT id,campaign_id,status,dispatch_state,dispatch_attempts,dispatch_attempt_id,provider_status,
       transfer_call_sid,transfer_status,outcome_reason,cost_reconcile_status
 FROM campaign_numbers ORDER BY id DESC LIMIT 10;
SELECT user_id,verification_status,verified_at FROM user_twilio_credentials ORDER BY user_id LIMIT 10;
SELECT caller_id_id,user_id,provider,status,checked_at FROM caller_id_provider_status ORDER BY caller_id_id LIMIT 10;
`;

execFileSync('npx', ['wrangler', 'd1', 'migrations', 'list', 'coldcalls', target], { cwd: root, stdio: 'inherit' });
execFileSync('npx', ['wrangler', 'd1', 'execute', 'coldcalls', target, '--command', sql], { cwd: root, stdio: 'inherit' });
