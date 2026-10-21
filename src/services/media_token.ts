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

function isValidAudioUrl(url: string): boolean {
  return /^https?:\/\//i.test(url);
}

export async function providerAudioUrl(env: Env, audioId: number): Promise<string> {
  const audio = await env.DB.prepare('SELECT r2_key, r2_url FROM audios WHERE id=? AND is_active=1')
    .bind(audioId).first<{ r2_key: string; r2_url: string }>();
  if (audio?.r2_url && isValidAudioUrl(audio.r2_url)) return audio.r2_url;
  const publicBase = (env.AUDIO_PUBLIC_URL ?? '').replace(/\/+$/, '');
  if (publicBase && audio?.r2_key) return `${publicBase}/${audio.r2_key}`;
  throw new Error(`Audio ${audioId} has no R2 URL configured`);
}
