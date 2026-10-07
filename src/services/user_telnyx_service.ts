import type { Env } from '../middleware';
import { decryptFernet, encryptFernet } from '../auth';

export interface UserTelnyxCredentialRow {
  id: number;
  user_id: number;
  api_key_encrypted: string;
  account_sid_encrypted: string;
  created_at: string;
  updated_at: string;
}

export async function getUserTelnyxCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<[string, string]> {
  const row = await db
    .prepare('SELECT * FROM user_telnyx_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserTelnyxCredentialRow>();
  if (!row) return ['', ''];
  try {
    return [
      decryptFernet(env.ENCRYPTION_KEY, row.api_key_encrypted),
      decryptFernet(env.ENCRYPTION_KEY, row.account_sid_encrypted),
    ];
  } catch {
    return ['', ''];
  }
}

export async function hasUserTelnyxCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<boolean> {
  const [apiKey, accountSid] = await getUserTelnyxCredentials(env, db, userId);
  return Boolean(apiKey && accountSid);
}

export async function upsertUserTelnyxCredentials(
  env: Env,
  db: D1Database,
  userId: number,
  apiKey: string,
  accountSid: string,
): Promise<UserTelnyxCredentialRow> {
  const encryptedApiKey = encryptFernet(env.ENCRYPTION_KEY, apiKey.trim());
  const encryptedAccountSid = encryptFernet(env.ENCRYPTION_KEY, accountSid.trim());
  const existing = await db
    .prepare('SELECT * FROM user_telnyx_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserTelnyxCredentialRow>();
  const now = new Date().toISOString();

  if (existing) {
    await db
      .prepare(
        'UPDATE user_telnyx_credentials SET api_key_encrypted = ?, account_sid_encrypted = ?, updated_at = ? WHERE user_id = ?',
      )
      .bind(encryptedApiKey, encryptedAccountSid, now, userId)
      .run();
    return {
      ...existing,
      api_key_encrypted: encryptedApiKey,
      account_sid_encrypted: encryptedAccountSid,
      updated_at: now,
    };
  }

  const row = await db
    .prepare(
      'INSERT INTO user_telnyx_credentials (user_id, api_key_encrypted, account_sid_encrypted) VALUES (?, ?, ?) RETURNING *',
    )
    .bind(userId, encryptedApiKey, encryptedAccountSid)
    .first<UserTelnyxCredentialRow>();
  if (!row) throw new Error('Failed to store Telnyx credentials');
  return row;
}
