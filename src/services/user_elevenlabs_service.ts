import type { Env } from '../middleware';
import { decryptFernet, encryptFernet } from '../auth';

export interface UserElevenLabsCredentialRow {
  id: number;
  user_id: number;
  api_key_encrypted: string;
  created_at: string;
  updated_at: string;
}

export async function getUserElevenlabsCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<string> {
  const row = await db
    .prepare('SELECT * FROM user_elevenlabs_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserElevenLabsCredentialRow>();
  if (!row) return '';
  try {
    return decryptFernet(env.ENCRYPTION_KEY, row.api_key_encrypted);
  } catch {
    return '';
  }
}

export async function hasUserElevenlabsCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<boolean> {
  return Boolean(await getUserElevenlabsCredentials(env, db, userId));
}

export async function upsertUserElevenlabsCredentials(
  env: Env,
  db: D1Database,
  userId: number,
  apiKey: string,
): Promise<UserElevenLabsCredentialRow> {
  const encryptedApiKey = encryptFernet(env.ENCRYPTION_KEY, apiKey.trim());
  const existing = await db
    .prepare('SELECT * FROM user_elevenlabs_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserElevenLabsCredentialRow>();
  const now = new Date().toISOString();

  if (existing) {
    await db
      .prepare(
        'UPDATE user_elevenlabs_credentials SET api_key_encrypted = ?, updated_at = ? WHERE user_id = ?',
      )
      .bind(encryptedApiKey, now, userId)
      .run();
    return { ...existing, api_key_encrypted: encryptedApiKey, updated_at: now };
  }

  const row = await db
    .prepare(
      'INSERT INTO user_elevenlabs_credentials (user_id, api_key_encrypted) VALUES (?, ?) RETURNING *',
    )
    .bind(userId, encryptedApiKey)
    .first<UserElevenLabsCredentialRow>();
  if (!row) throw new Error('Failed to store ElevenLabs credentials');
  return row;
}
