import type { Env } from '../middleware';
import { decryptFernet, encryptFernet } from '../auth';

export interface UserVonageCredentialRow {
  id: number;
  user_id: number;
  application_id_encrypted: string;
  private_key_encrypted: string;
  created_at: string;
  updated_at: string;
}

export async function getUserVonageCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<[string, string]> {
  const row = await db
    .prepare('SELECT * FROM user_vonage_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserVonageCredentialRow>();
  if (!row) return ['', ''];
  try {
    return [
      decryptFernet(env.ENCRYPTION_KEY, row.application_id_encrypted),
      decryptFernet(env.ENCRYPTION_KEY, row.private_key_encrypted),
    ];
  } catch {
    return ['', ''];
  }
}

export async function hasUserVonageCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<boolean> {
  const [applicationId, privateKey] = await getUserVonageCredentials(env, db, userId);
  return Boolean(applicationId && privateKey);
}

export async function upsertUserVonageCredentials(
  env: Env,
  db: D1Database,
  userId: number,
  applicationId: string,
  privateKey: string,
): Promise<UserVonageCredentialRow> {
  const encryptedAppId = encryptFernet(env.ENCRYPTION_KEY, applicationId.trim());
  const encryptedPrivateKey = encryptFernet(env.ENCRYPTION_KEY, privateKey.trim());
  const existing = await db
    .prepare('SELECT * FROM user_vonage_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserVonageCredentialRow>();
  const now = new Date().toISOString();

  if (existing) {
    await db
      .prepare(
        'UPDATE user_vonage_credentials SET application_id_encrypted = ?, private_key_encrypted = ?, updated_at = ? WHERE user_id = ?',
      )
      .bind(encryptedAppId, encryptedPrivateKey, now, userId)
      .run();
    return {
      ...existing,
      application_id_encrypted: encryptedAppId,
      private_key_encrypted: encryptedPrivateKey,
      updated_at: now,
    };
  }

  const row = await db
    .prepare(
      'INSERT INTO user_vonage_credentials (user_id, application_id_encrypted, private_key_encrypted) VALUES (?, ?, ?) RETURNING *',
    )
    .bind(userId, encryptedAppId, encryptedPrivateKey)
    .first<UserVonageCredentialRow>();
  if (!row) throw new Error('Failed to store Vonage credentials');
  return row;
}
