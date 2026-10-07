import type { Env } from '../middleware';
import { decryptFernet, encryptFernet } from '../auth';

export interface UserVoximplantCredentialRow {
  id: number;
  user_id: number;
  account_id_encrypted: string;
  application_id_encrypted: string | null;
  service_account_email_encrypted: string;
  key_id_encrypted: string;
  private_key_encrypted: string;
  vox_app_id: number | null;
  vox_rule_id: number | null;
  vox_scenario_id: number | null;
  provision_status: string;
  provision_error: string | null;
  provisioned_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface VoximplantCredentials {
  account_id: string;
  application_id: string;
  service_account_email: string;
  key_id: string;
  private_key: string;
  vox_app_id: number | null;
  vox_rule_id: number | null;
  vox_scenario_id: number | null;
  provision_status: string;
  provision_error: string | null;
}

export async function getUserVoximplantCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<VoximplantCredentials | null> {
  const row = await db
    .prepare('SELECT * FROM user_voximplant_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserVoximplantCredentialRow>();
  if (!row) return null;
  try {
    return {
      account_id: decryptFernet(env.ENCRYPTION_KEY, row.account_id_encrypted),
      application_id: row.application_id_encrypted
        ? decryptFernet(env.ENCRYPTION_KEY, row.application_id_encrypted)
        : '',
      service_account_email: decryptFernet(
        env.ENCRYPTION_KEY,
        row.service_account_email_encrypted,
      ),
      key_id: decryptFernet(env.ENCRYPTION_KEY, row.key_id_encrypted),
      private_key: decryptFernet(env.ENCRYPTION_KEY, row.private_key_encrypted),
      vox_app_id: row.vox_app_id,
      vox_rule_id: row.vox_rule_id,
      vox_scenario_id: row.vox_scenario_id,
      provision_status: row.provision_status,
      provision_error: row.provision_error,
    };
  } catch {
    return null;
  }
}

export async function hasUserVoximplantCredentials(
  env: Env,
  db: D1Database,
  userId: number,
): Promise<boolean> {
  const credentials = await getUserVoximplantCredentials(env, db, userId);
  if (!credentials) return false;
  return Boolean(
    credentials.account_id &&
      credentials.service_account_email &&
      credentials.key_id &&
      credentials.private_key,
  );
}

export interface UpsertVoximplantCredentialsInput {
  accountId: string;
  applicationId: string;
  serviceAccountEmail: string;
  keyId: string;
  privateKey: string;
}

export async function upsertUserVoximplantCredentials(
  env: Env,
  db: D1Database,
  userId: number,
  input: UpsertVoximplantCredentialsInput,
): Promise<UserVoximplantCredentialRow> {
  const encryptedAccountId = encryptFernet(env.ENCRYPTION_KEY, input.accountId.trim());
  const encryptedApplicationId = input.applicationId.trim()
    ? encryptFernet(env.ENCRYPTION_KEY, input.applicationId.trim())
    : null;
  const encryptedServiceAccountEmail = encryptFernet(
    env.ENCRYPTION_KEY,
    input.serviceAccountEmail.trim(),
  );
  const encryptedKeyId = encryptFernet(env.ENCRYPTION_KEY, input.keyId.trim());
  const encryptedPrivateKey = encryptFernet(env.ENCRYPTION_KEY, input.privateKey.trim());
  const now = new Date().toISOString();
  const existing = await db
    .prepare('SELECT * FROM user_voximplant_credentials WHERE user_id = ?')
    .bind(userId)
    .first<UserVoximplantCredentialRow>();

  if (existing) {
    await db
      .prepare(
        'UPDATE user_voximplant_credentials SET account_id_encrypted = ?, application_id_encrypted = ?, service_account_email_encrypted = ?, key_id_encrypted = ?, private_key_encrypted = ?, provision_status = ?, provision_error = ?, updated_at = ? WHERE user_id = ?',
      )
      .bind(
        encryptedAccountId,
        encryptedApplicationId,
        encryptedServiceAccountEmail,
        encryptedKeyId,
        encryptedPrivateKey,
        'pending',
        null,
        now,
        userId,
      )
      .run();
    return {
      ...existing,
      account_id_encrypted: encryptedAccountId,
      application_id_encrypted: encryptedApplicationId,
      service_account_email_encrypted: encryptedServiceAccountEmail,
      key_id_encrypted: encryptedKeyId,
      private_key_encrypted: encryptedPrivateKey,
      provision_status: 'pending',
      provision_error: null,
      updated_at: now,
    };
  }

  const row = await db
    .prepare(
      'INSERT INTO user_voximplant_credentials (user_id, account_id_encrypted, application_id_encrypted, service_account_email_encrypted, key_id_encrypted, private_key_encrypted, provision_status) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING *',
    )
    .bind(
      userId,
      encryptedAccountId,
      encryptedApplicationId,
      encryptedServiceAccountEmail,
      encryptedKeyId,
      encryptedPrivateKey,
      'pending',
    )
    .first<UserVoximplantCredentialRow>();
  if (!row) throw new Error('Failed to store Voximplant credentials');
  return row;
}
