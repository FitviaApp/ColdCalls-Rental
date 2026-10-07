import type { Env } from '../middleware';
import { decryptFernet, encryptFernet } from '../auth';

export interface UserOpenAICredentialRow {
  id: number;
  user_id: number;
  api_key_encrypted: string;
  organization_id_encrypted: string | null;
  created_at: string;
  updated_at: string;
}

export async function getUserOpenaiCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<[string, string]> {
  const row = await db
    .prepare('SELECT * FROM user_openai_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserOpenAICredentialRow>();
  if (!row) return ['', ''];
  try {
    return [
      decryptFernet(env.ENCRYPTION_KEY, row.api_key_encrypted),
      row.organization_id_encrypted
        ? decryptFernet(env.ENCRYPTION_KEY, row.organization_id_encrypted)
        : '',
    ];
  } catch {
    return ['', ''];
  }
}

export async function hasUserOpenaiCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<boolean> {
  const [apiKey] = await getUserOpenaiCredentials(env, db, userId);
  return Boolean(apiKey);
}

export async function upsertUserOpenaiCredentials(
  env: Env,
  db: D1Database,
  userId: number,
  apiKey: string,
  organizationId = '',
): Promise<UserOpenAICredentialRow> {
  const encryptedApiKey = encryptFernet(env.ENCRYPTION_KEY, apiKey.trim());
  const encryptedOrg = organizationId.trim()
    ? encryptFernet(env.ENCRYPTION_KEY, organizationId.trim())
    : null;
  const existing = await db
    .prepare('SELECT * FROM user_openai_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserOpenAICredentialRow>();
  const now = new Date().toISOString();

  if (existing) {
    await db
      .prepare(
        'UPDATE user_openai_credentials SET api_key_encrypted = ?, organization_id_encrypted = ?, updated_at = ? WHERE user_id = ?',
      )
      .bind(encryptedApiKey, encryptedOrg, now, userId)
      .run();
    return {
      ...existing,
      api_key_encrypted: encryptedApiKey,
      organization_id_encrypted: encryptedOrg,
      updated_at: now,
    };
  }

  const row = await db
    .prepare(
      'INSERT INTO user_openai_credentials (user_id, api_key_encrypted, organization_id_encrypted) VALUES (?, ?, ?) RETURNING *',
    )
    .bind(userId, encryptedApiKey, encryptedOrg)
    .first<UserOpenAICredentialRow>();
  if (!row) throw new Error('Failed to store OpenAI credentials');
  return row;
}
