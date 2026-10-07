import { SignJWT, jwtVerify } from 'jose';
import bcrypt from 'bcryptjs';
import { Buffer } from 'node:buffer';
import {
  createCipheriv,
  createDecipheriv,
  createHash,
  createHmac,
  randomBytes,
  timingSafeEqual,
} from 'node:crypto';

// ---- JWT (HS256, compatible with the previous python-jose tokens) ----

export async function signToken(secret: string, userId: number): Promise<string> {
  return new SignJWT({})
    .setProtectedHeader({ alg: 'HS256' })
    .setSubject(String(userId))
    .setIssuedAt()
    .setExpirationTime('24h')
    .sign(new TextEncoder().encode(secret));
}

export async function verifyToken(
  secret: string,
  token: string,
): Promise<{ sub: string } | null> {
  try {
    const { payload } = await jwtVerify(token, new TextEncoder().encode(secret));
    return typeof payload.sub === 'string' ? { sub: payload.sub } : null;
  } catch {
    return null;
  }
}

// ---- Passwords (bcrypt hashes from passlib keep working) ----

export function verifyPassword(plain: string, hash: string): boolean {
  return bcrypt.compareSync(plain, hash);
}

export function hashPassword(plain: string): string {
  return bcrypt.hashSync(plain, 10);
}

// ---- Fernet-compatible credential encryption (same format as python cryptography ----

function fernetKeyMaterial(encryptionKey: string): Buffer {
  const keyBytes = Buffer.from(encryptionKey, 'utf8');
  if (keyBytes.length < 32) {
    // Legacy path: seed padded with '=' to 32 bytes (python ljust(32, b'=')[:32]).
    const seed = Buffer.alloc(32, 0x3d);
    keyBytes.copy(seed);
    return seed;
  }
  try {
    const b64 = encryptionKey.replace(/-/g, '+').replace(/_/g, '/');
    const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
    const decoded = Buffer.from(padded, 'base64');
    if (decoded.length === 32) return decoded;
  } catch {
    // fall through to sha256 derivation
  }
  return createHash('sha256').update(keyBytes).digest();
}

function urlSafeBase64WithPadding(value: Buffer): string {
  let binary = '';
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_');
}

export function encryptFernet(encryptionKey: string, plaintext: string): string {
  const key = fernetKeyMaterial(encryptionKey);
  const iv = randomBytes(16);
  const signingKey = key.subarray(0, 16);
  const encryptionKeyBytes = key.subarray(16, 32);
  const cipher = createCipheriv('aes-128-cbc', encryptionKeyBytes, iv);
  const ct = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  const timestamp = Buffer.alloc(8);
  timestamp.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 1000)));
  const payload = Buffer.concat([Buffer.from([0x80]), timestamp, iv, ct]);
  const sig = createHmac('sha256', signingKey).update(payload).digest();
  return urlSafeBase64WithPadding(Buffer.concat([payload, sig]));
}

export function decryptFernet(encryptionKey: string, token: string): string {
  const key = fernetKeyMaterial(encryptionKey);
  const data = Buffer.from(token, 'base64url');
  if (data.length < 73) throw new Error('invalid token');
  const payload = data.subarray(0, data.length - 32);
  const sig = data.subarray(data.length - 32);
  const expected = createHmac('sha256', key.subarray(0, 16)).update(payload).digest();
  if (!timingSafeEqual(sig, expected)) throw new Error('invalid token');
  if (payload[0] !== 0x80) throw new Error('invalid token');
  const iv = payload.subarray(9, 25);
  const ct = payload.subarray(25);
  const decipher = createDecipheriv('aes-128-cbc', key.subarray(16, 32), iv);
  return Buffer.concat([decipher.update(ct), decipher.final()]).toString('utf8');
}
