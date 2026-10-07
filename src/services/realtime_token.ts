import { jwtVerify, SignJWT, jwtKey } from './jwt_key';
import type { Env } from '../middleware';

export async function createRealtimeToken(env: Env, campaignNumberId: number): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({ sub: String(campaignNumberId), purpose: 'twilio_media_stream' })
    .setProtectedHeader({ alg: 'HS256' })
    .setIssuedAt(now)
    .setExpirationTime(now + 7200)
    .sign(jwtKey(env));
}

export async function verifyRealtimeToken(env: Env, token: string, campaignNumberId: number): Promise<boolean> {
  try {
    const { payload } = await jwtVerify(token, jwtKey(env));
    return payload.purpose === 'twilio_media_stream' && Number(payload.sub) === campaignNumberId;
  } catch {
    return false;
  }
}
