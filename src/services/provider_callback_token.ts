import { jwtVerify, SignJWT, jwtKey } from './jwt_key';
import type { Env } from '../middleware';

export async function createProviderCallbackToken(
  env: Env,
  provider: string,
  campaignId: number,
  expiresInSeconds = 86400,
): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({ campaign_id: campaignId, provider, purpose: 'provider_callback' })
    .setProtectedHeader({ alg: 'HS256' }).setIssuedAt(now).setExpirationTime(now + expiresInSeconds)
    .sign(jwtKey(env));
}

export type TwilioCallbackPurpose = 'answer' | 'gather' | 'parent-status' | 'transfer-status';

export interface TwilioAttemptTokenPayload {
  numberId: number;
  attemptId: string;
  purpose: TwilioCallbackPurpose;
}

export async function createTwilioAttemptToken(
  env: Env,
  payload: TwilioAttemptTokenPayload,
): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({
    campaign_number_id: payload.numberId,
    dispatch_attempt_id: payload.attemptId,
    provider: 'twilio',
    purpose: payload.purpose,
  })
    .setProtectedHeader({ alg: 'HS256' })
    .setIssuedAt(now)
    .setExpirationTime(now + 86400)
    .sign(jwtKey(env));
}

export async function verifyTwilioAttemptToken(
  env: Env,
  token: string,
  expected: TwilioAttemptTokenPayload,
): Promise<boolean> {
  try {
    const { payload } = await jwtVerify(token, jwtKey(env));
    return payload.provider === 'twilio'
      && payload.purpose === expected.purpose
      && Number(payload.campaign_number_id) === expected.numberId
      && payload.dispatch_attempt_id === expected.attemptId;
  } catch {
    return false;
  }
}

export async function verifyProviderCallbackToken(env: Env, token: string, provider: string, campaignId: number): Promise<boolean> {
  try {
    const { payload } = await jwtVerify(token, jwtKey(env));
    return payload.purpose === 'provider_callback' && payload.provider === provider && Number(payload.campaign_id) === campaignId;
  } catch { return false; }
}
