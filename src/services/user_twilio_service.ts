import type { Env } from '../middleware';
import { decryptFernet, encryptFernet } from '../auth';

export interface UserTwilioCredentialRow {
  id: number;
  user_id: number;
  account_sid_encrypted: string;
  auth_token_encrypted: string;
  verification_status: string;
  verified_at: string | null;
  verification_error: string | null;
  created_at: string;
  updated_at: string;
}

export async function getUserTwilioCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<[string, string]> {
  const row = await db
    .prepare('SELECT * FROM user_twilio_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserTwilioCredentialRow>();
  if (!row) return ['', ''];
  try {
    return [
      decryptFernet(env.ENCRYPTION_KEY, row.account_sid_encrypted),
      decryptFernet(env.ENCRYPTION_KEY, row.auth_token_encrypted),
    ];
  } catch {
    return ['', ''];
  }
}

export async function hasUserTwilioCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<boolean> {
  const row = await db.prepare(
    'SELECT verification_status FROM user_twilio_credentials WHERE user_id = ?',
  ).bind(userId).first<{ verification_status: string }>();
  if (row?.verification_status !== 'verified') return false;
  const [accountSid, authToken] = await getUserTwilioCredentials(env, db, userId);
  return Boolean(accountSid && authToken);
}

export async function getUserTwilioVerification(
  db: D1Database,
  userId: number,
): Promise<Pick<UserTwilioCredentialRow, 'verification_status' | 'verified_at' | 'verification_error'> | null> {
  return db.prepare(
    'SELECT verification_status, verified_at, verification_error FROM user_twilio_credentials WHERE user_id = ?',
  ).bind(userId).first();
}

export async function recordUserTwilioVerificationError(
  db: D1Database,
  userId: number,
  error: string,
): Promise<void> {
  await db.prepare(
    'UPDATE user_twilio_credentials SET verification_error=?, updated_at=? WHERE user_id=?',
  ).bind(error.slice(0, 350), new Date().toISOString(), userId).run();
}

export async function upsertUserTwilioCredentials(
  env: Env,
  db: D1Database,
  userId: number,
  accountSid: string,
  authToken: string,
  verification: { status: 'verified'; verifiedAt: string },
): Promise<UserTwilioCredentialRow> {
  const encryptedSid = encryptFernet(env.ENCRYPTION_KEY, accountSid.trim());
  const encryptedToken = encryptFernet(env.ENCRYPTION_KEY, authToken.trim());
  const existing = await db
    .prepare('SELECT * FROM user_twilio_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserTwilioCredentialRow>();
  const now = new Date().toISOString();

  if (existing) {
    await db
      .prepare(
        `UPDATE user_twilio_credentials SET account_sid_encrypted = ?, auth_token_encrypted = ?,
         verification_status = ?, verified_at = ?, verification_error = NULL, updated_at = ? WHERE user_id = ?`,
      )
      .bind(encryptedSid, encryptedToken, verification.status, verification.verifiedAt, now, userId)
      .run();
    return {
      ...existing,
      account_sid_encrypted: encryptedSid,
      auth_token_encrypted: encryptedToken,
      verification_status: verification.status,
      verified_at: verification.verifiedAt,
      verification_error: null,
      updated_at: now,
    };
  }

  const row = await db
    .prepare(
      `INSERT INTO user_twilio_credentials
       (user_id, account_sid_encrypted, auth_token_encrypted, verification_status, verified_at, verification_error)
       VALUES (?, ?, ?, ?, ?, NULL) RETURNING *`,
    )
    .bind(userId, encryptedSid, encryptedToken, verification.status, verification.verifiedAt)
    .first<UserTwilioCredentialRow>();
  if (!row) throw new Error('Failed to store Twilio credentials');
  return row;
}
