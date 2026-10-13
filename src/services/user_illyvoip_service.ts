import type { Env } from '../middleware';
import { decryptFernet, encryptFernet } from '../auth';

export interface UserIllyvoipCredentialRow {
  id: number;
  user_id: number;
  api_key_encrypted: string;
  created_at: string;
  updated_at: string;
}

export async function getUserIllyvoipCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<string> {
  const row = await db
    .prepare('SELECT * FROM user_illyvoip_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserIllyvoipCredentialRow>();
  if (!row) return '';
  try {
    return decryptFernet(env.ENCRYPTION_KEY, row.api_key_encrypted);
  } catch {
    return '';
  }
}

export async function hasUserIllyvoipCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<boolean> {
  return Boolean(await getUserIllyvoipCredentials(env, db, userId));
}

export async function upsertUserIllyvoipCredentials(
  env: Env,
  db: D1Database,
  userId: number,
  apiKey: string,
): Promise<UserIllyvoipCredentialRow> {
  const encryptedApiKey = encryptFernet(env.ENCRYPTION_KEY, apiKey.trim());
  const existing = await db
    .prepare('SELECT * FROM user_illyvoip_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserIllyvoipCredentialRow>();
  const now = new Date().toISOString();

  if (existing) {
    await db
      .prepare(
        'UPDATE user_illyvoip_credentials SET api_key_encrypted = ?, updated_at = ? WHERE user_id = ?',
      )
      .bind(encryptedApiKey, now, userId)
      .run();
    return { ...existing, api_key_encrypted: encryptedApiKey, updated_at: now };
  }

  const row = await db
    .prepare(
      'INSERT INTO user_illyvoip_credentials (user_id, api_key_encrypted) VALUES (?, ?) RETURNING *',
    )
    .bind(userId, encryptedApiKey)
    .first<UserIllyvoipCredentialRow>();
  if (!row) throw new Error('Failed to store IllyVoIP credentials');
  return row;
}
