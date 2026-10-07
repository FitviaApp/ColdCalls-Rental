import { jwtVerify, SignJWT } from 'jose';
import type { Env } from '../middleware';

export { jwtVerify, SignJWT };

export function jwtKey(env: Env): Uint8Array {
  if (!env.JWT_SECRET || env.JWT_SECRET === 'undefined') {
    throw new Error('JWT_SECRET is not configured');
  }
  return new TextEncoder().encode(env.JWT_SECRET);
}
