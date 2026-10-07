import { jwtVerify, SignJWT, jwtKey } from './jwt_key';
import type { Env } from '../middleware';

export async function createAudioToken(env: Env, audioId: number, expiresInSeconds = 7200): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({ audio_id: audioId, purpose: 'provider_audio' })
    .setProtectedHeader({ alg: 'HS256' })
    .setIssuedAt(now)
    .setExpirationTime(now + expiresInSeconds)
    .sign(jwtKey(env));
}

export async function verifyAudioToken(env: Env, token: string, audioId: number): Promise<boolean> {
  try {
    const { payload } = await jwtVerify(token, jwtKey(env));
    return payload.purpose === 'provider_audio' && Number(payload.audio_id) === audioId;
  } catch {
    return false;
  }
}

export async function providerAudioUrl(env: Env, audioId: number): Promise<string> {
  const token = await createAudioToken(env, audioId);
  return `${(env.BASE_URL ?? '').replace(/\/+$/, '')}/api/media/audio/${audioId}?token=${encodeURIComponent(token)}`;
}
