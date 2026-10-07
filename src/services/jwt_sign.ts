import { importPKCS8, SignJWT } from 'jose';
import type { JWTPayload } from 'jose';

function decodeBase64(value: string): Uint8Array {
  return Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
}

function encodeBase64(value: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < value.length; i += 0x8000) {
    binary += String.fromCharCode(...value.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

function encodeLength(length: number): number[] {
  if (length < 0x80) return [length];
  if (length < 0x100) return [0x81, length];
  return [0x82, (length >> 8) & 0xff, length & 0xff];
}

function wrapPkcs1(der: Uint8Array): Uint8Array {
  const algorithm = [
    0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00,
  ];
  const octet = [0x04, ...encodeLength(der.length), ...der];
  const inner = [0x02, 0x01, 0x00, ...algorithm, ...octet];
  return Uint8Array.from([0x30, ...encodeLength(inner.length), ...inner]);
}

function normalizePrivateKeyPem(pem: string): string {
  const trimmed = String(pem ?? '').trim();
  if (trimmed.includes('BEGIN PRIVATE KEY')) return trimmed;
  const match = /-----BEGIN RSA PRIVATE KEY-----([\s\S]*?)-----END RSA PRIVATE KEY-----/.exec(
    trimmed,
  );
  if (!match) return trimmed;
  const der = decodeBase64(match[1].replace(/\s+/g, ''));
  const wrapped = encodeBase64(wrapPkcs1(der))
    .replace(/(.{64})/g, '$1\n')
    .trim();
  return `-----BEGIN PRIVATE KEY-----\n${wrapped}\n-----END PRIVATE KEY-----`;
}

export async function signRs256Jwt(
  privateKeyPem: string,
  payload: JWTPayload,
  headers?: Record<string, unknown>,
): Promise<string> {
  const key = await importPKCS8(normalizePrivateKeyPem(privateKeyPem), 'RS256');
  return new SignJWT(payload).setProtectedHeader({ alg: 'RS256', ...headers }).sign(key);
}
