// One-time D1 seed: admin user + default rental plans.
// The password is requested without echo and is never written to the repository.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import bcrypt from 'bcryptjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const email = String(process.env.ADMIN_EMAIL ?? '').trim().toLowerCase();
if (!/^\S+@\S+\.\S+$/.test(email)) throw new Error('Set ADMIN_EMAIL for the new administrator.');
const password = await readSecret('New admin password: ');
if (password.length < 12) throw new Error('Admin password must contain at least 12 characters.');
const hash = bcrypt.hashSync(password, 10);
const q = (s) => `'${String(s).replace(/'/g, "''")}'`;

const sql = `
INSERT OR IGNORE INTO users (email, password_hash, is_admin, is_active)
VALUES (${q(email)}, ${q(hash)}, 1, 1);
INSERT OR IGNORE INTO rental_plans (code, name, duration_days, price_usdt, is_active)
VALUES ('daily', 'Daily Rental', 1, 15.0, 1);
INSERT OR IGNORE INTO rental_plans (code, name, duration_days, price_usdt, is_active)
VALUES ('weekly', 'Weekly Rental', 7, 75.0, 1);
`;

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'coldcalls-bootstrap-'));
const tmp = path.join(tmpDir, 'bootstrap.sql');
fs.writeFileSync(tmp, sql, { mode: 0o600 });
try {
  const target = process.argv.includes('--local') ? '--local' : '--remote';
  execFileSync('npx', ['wrangler', 'd1', 'execute', 'coldcalls', target, '--file', tmp], {
    cwd: root,
    stdio: 'inherit',
  });
  execFileSync('npx', ['wrangler', 'd1', 'execute', 'coldcalls', target, '--command',
    `SELECT email,is_admin,is_active FROM users WHERE email=${q(email)}; SELECT code,duration_days,price_usdt FROM rental_plans ORDER BY code;`],
  { cwd: root, stdio: 'inherit' });
  console.log(`bootstrap and readback done (admin: ${email})`);
} finally {
  fs.unlinkSync(tmp);
  fs.rmdirSync(tmpDir);
}

function readSecret(prompt) {
  if (!process.stdin.isTTY || typeof process.stdin.setRawMode !== 'function') {
    throw new Error('Run this command in an interactive terminal so the password can be entered securely.');
  }
  return new Promise((resolve, reject) => {
    process.stdout.write(prompt);
    let value = '';
    process.stdin.setRawMode(true);
    process.stdin.resume();
    const done = () => {
      process.stdin.off('data', onData);
      process.stdin.setRawMode(false);
      process.stdin.pause();
      process.stdout.write('\n');
      resolve(value);
    };
    const onData = (buffer) => {
      for (const byte of buffer) {
        if (byte === 3) { process.stdout.write('\n'); reject(new Error('Cancelled')); return; }
        if (byte === 13 || byte === 10) { done(); return; }
        if (byte === 127 || byte === 8) value = value.slice(0, -1);
        else if (byte >= 32) value += String.fromCharCode(byte);
      }
    };
    process.stdin.on('data', onData);
  });
}
