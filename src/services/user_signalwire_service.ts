import type { Env } from '../middleware';
import { decryptFernet, encryptFernet } from '../auth';

export interface UserSignalWireCredentialRow {
  id: number;
  user_id: number;
  project_id_encrypted: string;
  api_token_encrypted: string;
  space_url_encrypted: string;
  created_at: string;
  updated_at: string;
}

function normalizeSpaceUrl(spaceUrl: string): string {
  let normalized = String(spaceUrl ?? '').trim();
  if (!normalized) return '';
  if (normalized.startsWith('https://')) normalized = normalized.slice('https://'.length);
  else if (normalized.startsWith('http://')) normalized = normalized.slice('http://'.length);
  return normalized.replace(/\/+$/, '');
}

export async function getUserSignalwireCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<[string, string, string]> {
  const row = await db
    .prepare('SELECT * FROM user_signalwire_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserSignalWireCredentialRow>();
  if (!row) return ['', '', ''];
  try {
    return [
      decryptFernet(env.ENCRYPTION_KEY, row.project_id_encrypted),
      decryptFernet(env.ENCRYPTION_KEY, row.api_token_encrypted),
      normalizeSpaceUrl(decryptFernet(env.ENCRYPTION_KEY, row.space_url_encrypted)),
    ];
  } catch {
    return ['', '', ''];
  }
}

export async function hasUserSignalwireCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<boolean> {
  const [projectId, apiToken, spaceUrl] = await getUserSignalwireCredentials(env, db, userId);
  return Boolean(projectId && apiToken && spaceUrl);
}

export async function upsertUserSignalwireCredentials(
  env: Env,
  db: D1Database,
  userId: number,
  projectId: string,
  apiToken: string,
  spaceUrl: string,
): Promise<UserSignalWireCredentialRow> {
  const normalizedSpaceUrl = normalizeSpaceUrl(spaceUrl);
  const encryptedProjectId = encryptFernet(env.ENCRYPTION_KEY, projectId.trim());
  const encryptedApiToken = encryptFernet(env.ENCRYPTION_KEY, apiToken.trim());
  const encryptedSpaceUrl = encryptFernet(env.ENCRYPTION_KEY, normalizedSpaceUrl);
  const existing = await db
    .prepare('SELECT * FROM user_signalwire_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserSignalWireCredentialRow>();
  const now = new Date().toISOString();

  if (existing) {
    await db
      .prepare(
        'UPDATE user_signalwire_credentials SET project_id_encrypted = ?, api_token_encrypted = ?, space_url_encrypted = ?, updated_at = ? WHERE user_id = ?',
      )
      .bind(encryptedProjectId, encryptedApiToken, encryptedSpaceUrl, now, userId)
      .run();
    return {
      ...existing,
      project_id_encrypted: encryptedProjectId,
      api_token_encrypted: encryptedApiToken,
      space_url_encrypted: encryptedSpaceUrl,
      updated_at: now,
    };
  }

  const row = await db
    .prepare(
      'INSERT INTO user_signalwire_credentials (user_id, project_id_encrypted, api_token_encrypted, space_url_encrypted) VALUES (?, ?, ?, ?) RETURNING *',
    )
    .bind(userId, encryptedProjectId, encryptedApiToken, encryptedSpaceUrl)
    .first<UserSignalWireCredentialRow>();
  if (!row) throw new Error('Failed to store SignalWire credentials');
  return row;
}
